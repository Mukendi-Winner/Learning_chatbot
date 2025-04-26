import os
import json
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from together import Together
import PyPDF2
from werkzeug.utils import secure_filename
import uuid
from threading import Lock
import logging
import re

app = Flask(__name__)
CORS(app)

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
UPLOAD_FOLDER = 'Uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'pdf'}
SESSION_STORAGE = {}
LOCK = Lock()

# Configurer Together AI
api_key = ""
client = Together(api_key=api_key)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_pdf_text(filepath):
    """
    Extraire le texte page par page et détecter les sections (ex. chapitres, articles).
    Retourne une liste de dictionnaires {page_number, text, sections}.
    """
    try:
        with open(filepath, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            pages = []
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text() or ''
                logger.info(f"Page {i+1} extraite, longueur: {len(page_text)} caractères")
                # Détecter les sections (ex. "Chapitre 1", "Article 3")
                sections = []
                section_matches = re.finditer(r'(?i)\b(chapitre|chapter|article|section)\s+(\d+|[IVXLCDM]+)\b.*?(?=\b(chapitre|chapter|article|section)\s+\d+|$)', page_text, re.DOTALL)
                for match in section_matches:
                    sections.append({
                        'title': match.group(0).split('\n')[0].strip(),
                        'start': match.start(),
                        'end': match.end()
                    })
                pages.append({
                    'page_number': i + 1,
                    'text': page_text,
                    'sections': sections
                })
            if not any(page['text'].strip() for page in pages):
                logger.warning("Aucun texte extrait du PDF")
                return "Aucun texte lisible n'a été extrait du PDF."
            logger.info(f"Nombre de pages extraites: {len(pages)}")
            return pages
    except Exception as e:
        logger.error(f"Erreur extraction PDF : {str(e)}")
        return f"Erreur lors de l'extraction du texte : {str(e)}"

def summarize_pages(pages):
    """
    Résumer une liste de pages si elles dépassent 6000 caractères.
    """
    try:
        text = ''.join(page['text'] for page in pages)[:10000]
        summary_prompt = f"""
        Résumez le contenu suivant en 500 mots maximum, en conservant les points clés : {text}
        """
        response = client.chat.completions.create(
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
            messages=[{"role": "user", "content": summary_prompt}],
            temperature=0.7,
            max_tokens=750
        )
        summary = response.choices[0].message.content
        logger.info(f"Résumé généré, longueur: {len(summary)} caractères")
        return summary
    except Exception as e:
        logger.error(f"Erreur lors du résumé des pages : {str(e)}")
        return ''.join(page['text'] for page in pages)[:6000]

def get_session_id():
    session_id = request.json.get('session_id') if request.is_json else None
    if not session_id:
        session_id = request.form.get('session_id') or request.headers.get('X-Session-ID', str(uuid.uuid4()))
    logger.info(f"Session ID utilisé: {session_id}")
    return session_id

@app.route('/')
def home():
    logger.info("Accès à la page d'accueil")
    return render_template('homepage.html')

@app.route('/chat.html')
def chat_page():
    logger.info("Accès à la page de chat")
    return render_template('chat.html')

@app.route('/upload_pdf', methods=['POST'])
def upload_pdf():
    session_id = get_session_id()
    logger.info(f"Upload PDF pour session {session_id}")
    try:
        if 'pdf' not in request.files:
            logger.error("Aucun fichier PDF fourni")
            return jsonify({'error': 'Aucun fichier PDF fourni'}), 400
        file = request.files['pdf']
        if file.filename == '':
            logger.error("Aucun fichier sélectionné")
            return jsonify({'error': 'Aucun fichier sélectionné'}), 400
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{session_id}_{filename}")
            file.save(filepath)

            # Extraire le texte page par page
            pdf_pages = extract_pdf_text(filepath)
            if isinstance(pdf_pages, str):  # Erreur
                return jsonify({'error': pdf_pages}), 500

            # Stocker les pages dans la session
            with LOCK:
                if session_id not in SESSION_STORAGE:
                    SESSION_STORAGE[session_id] = {'pdfs': [], 'last_pdf': None}
                SESSION_STORAGE[session_id]['pdfs'].append({
                    'filename': filename,
                    'pages': pdf_pages
                })
                SESSION_STORAGE[session_id]['last_pdf'] = filename
                logger.info(f"PDFs stockés pour session {session_id}: {[pdf['filename'] for pdf in SESSION_STORAGE[session_id]['pdfs']]}, dernier PDF: {filename}")

            logger.info(f"PDF {filename} chargé avec succès")
            return jsonify({'message': f'PDF {filename} chargé avec succès. Posez des questions ou demandez un quiz (ex. "Résumez le chapitre 1", "Quiz sur les pages 10-20") !'})
        else:
            logger.error("Type de fichier non autorisé")
            return jsonify({'error': 'Type de fichier non autorisé'}), 400
    except Exception as e:
        logger.error(f"Erreur upload PDF : {str(e)}")
        return jsonify({'error': f'Erreur lors du chargement du PDF : {str(e)}'}), 500

@app.route('/chat', methods=['POST'])
def handle_chat():
    session_id = get_session_id()
    logger.info(f"Requête chat pour session {session_id}")
    try:
        data = request.get_json()
        if not data or 'message' not in data:
            logger.error("Le champ 'message' est requis")
            return jsonify({'error': 'Le champ "message" est requis'}), 400
        user_message = data['message']
        user_message_lower = user_message.lower()

        # Récupérer les PDF de la session
        with LOCK:
            session_data = SESSION_STORAGE.get(session_id, {'pdfs': [], 'last_pdf': None})
            pdfs = session_data['pdfs']
            last_pdf_filename = session_data.get('last_pdf')
            logger.info(f"PDFs stockés pour session {session_id}: {[pdf['filename'] for pdf in pdfs]}, dernier PDF: {last_pdf_filename}")

        # Sélectionner le PDF cible
        target_pdf = None
        target_filename = None
        for pdf in pdfs:
            if pdf['filename'].lower() in user_message_lower:
                target_pdf = pdf['pages']
                target_filename = pdf['filename']
                break
        if not target_pdf and last_pdf_filename:
            for pdf in pdfs:
                if pdf['filename'] == last_pdf_filename:
                    target_pdf = pdf['pages']
                    target_filename = pdf['filename']
                    break

        has_pdf = target_pdf is not None and isinstance(target_pdf, list) and any(page['text'].strip() for page in target_pdf)
        logger.info(f"PDF cible: {target_filename if target_filename else 'Aucun'}, disponible: {has_pdf}")

        # Déterminer le type de requête
        prompt = ''
        if has_pdf:
            # Sélectionner les pages pertinentes
            selected_pages = []
            total_length = 0
            max_length = 6000  # Limite pour respecter les 8193 tokens

            # Vérifier si l'utilisateur spécifie des pages (ex. "pages 10-20")
            page_range_match = re.search(r'pages?\s+(\d+)(?:-(\d+))?', user_message_lower)
            if page_range_match:
                start_page = int(page_range_match.group(1))
                end_page = int(page_range_match.group(2)) if page_range_match.group(2) else start_page
                selected_pages = [page for page in target_pdf if start_page <= page['page_number'] <= end_page]
                logger.info(f"Pages sélectionnées via intervalle: {start_page}-{end_page}")

            # Vérifier si l'utilisateur spécifie une section (ex. "chapitre 1", "article 3")
            section_match = re.search(r'(chapitre|chapter|article|section)\s+(\d+|[IVXLCDM]+)', user_message_lower)
            if section_match and not selected_pages:
                section_title = section_match.group(0)
                for page in target_pdf:
                    if any(section['title'].lower().startswith(section_title) for section in page['sections']):
                        selected_pages.append(page)
                        # Inclure les pages suivantes jusqu'à la prochaine section
                        next_pages = [p for p in target_pdf if p['page_number'] > page['page_number']]
                        for next_page in next_pages:
                            if next_page['sections']:
                                break
                            selected_pages.append(next_page)
                        break
                logger.info(f"Pages sélectionnées via section: {section_title}")

            # Si aucune sélection, utiliser les premières pages
            if not selected_pages:
                selected_pages = target_pdf[:10]  # Limite initiale à 10 pages
                logger.info("Aucune section ou intervalle spécifié, utilisation des premières pages")

            # Limiter la taille du texte
            target_text = ''
            for page in selected_pages:
                if total_length + len(page['text']) <= max_length:
                    target_text += page['text']
                    total_length += len(page['text'])
                else:
                    remaining = max_length - total_length
                    target_text += page['text'][:remaining]
                    total_length = max_length
                    break
            logger.info(f"Texte sélectionné, longueur: {total_length} caractères")

            # Résumer si le texte est encore trop long
            if total_length > max_length:
                logger.info(f"Texte trop long ({total_length} caractères), génération d'un résumé")
                target_text = summarize_pages(selected_pages)
                total_length = len(target_text)

            if 'quiz' in user_message_lower or 'test' in user_message_lower:
                prompt = f"""
                Basé sur le contenu suivant extrait du PDF '{target_filename}' : {target_text},
                générez un quiz de 5 questions à choix multiples (4 options par question) avec les réponses correctes.
                Formattez la réponse comme suit :
                Question 1: [Question]
                A) [Option]
                B) [Option]
                C) [Option]
                D) [Option]
                Donne la réponse correcte que quand on te le demande
                """
            elif any(keyword in user_message_lower for keyword in ['pdf', 'document', 'paper', 'abstract', 'summary', 'résumé', 'section', 'chapter']):
                prompt = f"""
                Basé sur le contenu suivant extrait du PDF '{target_filename}' : {target_text},
                répondez à la question suivante : {user_message}
                Fournissez une réponse claire et concise basée uniquement sur le contenu du PDF.
                Si la question ne correspond à aucune information du PDF, dites : "Désolé, je n'ai pas trouvé cette information dans le PDF."
                """
            else:
                prompt = f"""
                Vous êtes un assistant académique pour les étudiants universitaires.
                Répondez à la question suivante de manière claire et concise : {user_message}
                """
        else:
            prompt = f"""
            Vous êtes un assistant académique pour les étudiants universitaires.
            Répondez à la question suivante de manière claire et concise : {user_message}
            """

        # Vérifier la taille du prompt
        prompt_length = len(prompt)
        max_prompt_length = 7000  # Environ 5000 tokens, réserve pour réponse et instructions
        logger.info(f"Taille du prompt: {prompt_length} caractères (~{prompt_length//0.75} tokens)")
        if prompt_length > max_prompt_length:
            logger.warning(f"Prompt trop long ({prompt_length} caractères), réduction du texte")
            excess = prompt_length - max_prompt_length
            target_text = target_text[:len(target_text) - excess]
            prompt = prompt.replace(target_text, target_text[:len(target_text) - excess])
            logger.info(f"Nouvelle taille du prompt: {len(prompt)} caractères (~{len(prompt)//0.75} tokens)")

        logger.info(f"Type de prompt: {'Quiz' if 'quiz' in user_message_lower or 'test' in user_message_lower else 'PDF' if has_pdf and any(keyword in user_message_lower for keyword in ['pdf', 'document', 'paper', 'abstract', 'summary', 'résumé', 'section', 'chapter']) else 'Générale'}")
        logger.info(f"Prompt envoyé: {prompt[:200]}...")

        # Appeler Together AI
        response = client.chat.completions.create(
            model="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=1024
        )
        bot_response = response.choices[0].message.content

        logger.info(f"Réponse générée: {bot_response[:200]}...")
        return jsonify({'response': bot_response})
    except Exception as e:
        logger.error(f"Erreur chat : {str(e)}")
        return jsonify({'error': f'Erreur lors du traitement de la requête : {str(e)}'}), 500
    

@app.route('/get_sections', methods=['POST'])
def get_sections():
    session_id = get_session_id()
    logger.info(f"Demande de sections pour session {session_id}")
    try:
        data = request.get_json()
        if not data or 'filename' not in data:
            logger.error("Le champ 'filename' est requis")
            return jsonify({'error': 'Le champ "filename" est requis'}), 400
        filename = data['filename']

        with LOCK:
            session_data = SESSION_STORAGE.get(session_id, {'pdfs': [], 'last_pdf': None})
            pdfs = session_data['pdfs']
            target_pdf = None
            for pdf in pdfs:
                if pdf['filename'] == filename:
                    target_pdf = pdf['pages']
                    break
            if not target_pdf:
                logger.error(f"PDF {filename} non trouvé")
                return jsonify({'error': f'PDF {filename} non trouvé'}), 404

        sections = []
        for page in target_pdf:
            for section in page['sections']:
                sections.append({
                    'title': section['title'],
                    'page': page['page_number']
                })
        logger.info(f"Sections trouvées pour {filename}: {len(sections)}")
        return jsonify({'sections': sections})
    except Exception as e:
        logger.error(f"Erreur get_sections : {str(e)}")
        return jsonify({'error': f'Erreur lors de la récupération des sections : {str(e)}'}), 500




if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
import os, json, base64
from datetime import datetime
from collections import defaultdict

from flask import Flask, request, render_template, redirect, url_for, flash, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_socketio import SocketIO

# ------------------ Firebase Admin (lazy init) ------------------
import firebase_admin
from firebase_admin import credentials, firestore
from flask_cors import CORS

_db = None

def _load_firebase_creds():
    """Load credentials from FIREBASE_CREDENTIALS_JSON or FIREBASE_CREDENTIALS_B64."""
    raw = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass  # fall through to B64
    raw_b64 = os.environ.get("FIREBASE_CREDENTIALS_B64")
    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            return json.loads(decoded)
        except Exception as e:
            raise RuntimeError("Invalid FIREBASE_CREDENTIALS_B64") from e
    raise RuntimeError("Set FIREBASE_CREDENTIALS_JSON or FIREBASE_CREDENTIALS_B64")

def get_db():
    """Initialize Firebase once and return the Firestore client."""
    global _db
    if _db is None:
        creds_dict = _load_firebase_creds()
        cred = credentials.Certificate(creds_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        _db = firestore.client()
    return _db


# ------------------ Flask / SocketIO ------------------
app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')

# Respect Render/Cloudflare proxy headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

# Use threading backend (no gevent/eventlet conflicts)
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")


# ================== STORAGE HELPERS (Firestore) ==================

def create_question_set(name, class_name, level):
    db = get_db()
    doc_ref = db.collection("question_sets").document()  # auto-id
    doc_ref.set({
        "name": name,
        "class": class_name,
        "level": level,
        "created_at": firestore.SERVER_TIMESTAMP
    })
    return doc_ref.id

def list_question_sets():
    db = get_db()
    qs = db.collection("question_sets").order_by(
        "created_at", direction=firestore.Query.DESCENDING
    ).stream()
    out = []
    for doc in qs:
        data = doc.to_dict() or {}
        out.append((
            doc.id,
            data.get("name"),
            data.get("class"),
            data.get("level"),
            data.get("created_at"),
        ))
    return out

def add_question(set_id, q, a, b, c_, d, correct, idx):
    db = get_db()
    db.collection("question_sets").document(set_id)\
      .collection("questions").add({
          "question": q,
          "option_a": a,
          "option_b": b,
          "option_c": c_,
          "option_d": d,
          "correct": correct,
          "idx": idx
      })

def get_questions(set_id):
    db = get_db()
    docs = db.collection("question_sets").document(set_id)\
             .collection("questions").order_by("idx").stream()
    questions = []
    for d in docs:
        x = d.to_dict() or {}
        questions.append({
            "id": d.id,
            "question": x.get("question", ""),
            "options": {
                "A": x.get("option_a", ""),
                "B": x.get("option_b", ""),
                "C": x.get("option_c", ""),
                "D": x.get("option_d", "")
            },
            "correct": x.get("correct", "A"),
            "responses": {"A": [], "B": [], "C": [], "D": []}
        })
    return questions

def reset_questions(set_id):
    db = get_db()
    sub = db.collection("question_sets").document(set_id).collection("questions")
    batch = db.batch()
    count = 0
    for doc in sub.stream():
        batch.delete(doc.reference)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()

def delete_set(set_id):
    reset_questions(set_id)
    db = get_db()
    db.collection("question_sets").document(set_id).delete()

def record_response(set_id, question_id, student, option, is_correct):
    db = get_db()
    db.collection("responses").add({
        "set_id": set_id,
        "question_id": question_id,
        "student": student,
        "answer": option,
        "is_correct": is_correct,
        "timestamp": firestore.SERVER_TIMESTAMP
    })

def question_set_details(set_id):
    db = get_db()
    doc = db.collection("question_sets").document(set_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict() or {}
    return (d.get("name"), d.get("class"), d.get("level"))

def count_questions(set_id):
    db = get_db()
    return sum(1 for _ in db.collection("question_sets").document(set_id)
                 .collection("questions").stream())

def correct_count_for_student_in_set(student, set_id):
    db = get_db()
    snaps = db.collection("responses")\
              .where("student", "==", student)\
              .where("set_id", "==", set_id)\
              .where("is_correct", "==", True)\
              .stream()
    return sum(1 for _ in snaps)

def question_analysis_data(set_id):
    db = get_db()
    out = []
    for qdoc in db.collection("question_sets").document(set_id)\
                  .collection("questions").stream():
        qid = qdoc.id
        q = qdoc.to_dict() or {}
        total = sum(1 for _ in db.collection("responses")
                    .where("question_id", "==", qid).stream())
        corrects = sum(1 for _ in db.collection("responses")
                       .where("question_id", "==", qid)
                       .where("is_correct", "==", True).stream())
        out.append((qid, q.get("question", ""), q.get("correct", "A"), total, corrects))
    return out


# ================== IN-MEMORY STATE ==================
active_quiz = None
current_question_index = -1
student_scores = defaultdict(int)
current_set_id = None


# ================== ROUTES ==================

def get_question_sets():
    return list_question_sets()

@app.route('/healthz')
def healthz():
    return "ok", 200

@app.route('/__firetest')
def firetest():
    try:
        db = get_db()
        next(db.collection("question_sets").limit(1).stream(), None)
        return "firestore ok", 200
    except Exception as e:
        return f"firestore error: {e}", 500

@app.route('/')
def home():
    question_sets = get_question_sets()
    return render_template('index.html', question_sets=question_sets)

@app.route('/create_question_set', methods=['POST'])
def create_question_set_route():
    name = request.form.get('name')
    class_name = request.form.get('class')
    level = request.form.get('level')
    set_id = create_question_set(name, class_name, level)
    return redirect(url_for('upload_questions', set_id=set_id))

@app.route('/upload_questions/<set_id>')
def upload_questions(set_id):
    return render_template('upload.html', set_id=set_id)

@app.route('/save_questions/<set_id>', methods=['POST'])
def save_questions(set_id):
    num_questions = int(request.form.get('num_questions', 0))
    for i in range(num_questions):
        q = request.form.get(f'question_{i}')
        a = request.form.get(f'A_{i}')
        b = request.form.get(f'B_{i}')
        c_ = request.form.get(f'C_{i}')
        d = request.form.get(f'D_{i}')
        correct = (request.form.get(f'correct_{i}') or 'A').upper()
        if q and a and b and c_ and d and correct:
            add_question(set_id, q, a, b, c_, d, correct, idx=i)
    return redirect(url_for('home'))

@app.route('/edit_quiz/<set_id>')
def edit_quiz(set_id):
    qs = question_set_details(set_id)
    questions = get_questions(set_id)
    if not qs:
        flash('Question set not found.', 'error')
        return redirect(url_for('home'))
    # (id, name, class, level, created_at) for your template
    question_set = (set_id, qs[0], qs[1], qs[2], None)
    return render_template('edit_quiz.html', question_set=question_set, questions=questions)

@app.route('/update_quiz/<set_id>', methods=['POST'])
def update_quiz(set_id):
    db = get_db()
    # Update metadata
    db.collection("question_sets").document(set_id).set({
        "name": request.form.get('name'),
        "class": request.form.get('class'),
        "level": request.form.get('level'),
    }, merge=True)

    # Replace all questions with updated set
    reset_questions(set_id)
    num_questions = int(request.form.get('num_questions', 0))
    for i in range(num_questions):
        q = request.form.get(f'question_{i}')
        a = request.form.get(f'A_{i}')
        b = request.form.get(f'B_{i}')
        c_ = request.form.get(f'C_{i}')
        d = request.form.get(f'D_{i}')
        correct = (request.form.get(f'correct_{i}') or 'A').upper()
        if q and a and b and c_ and d and correct:
            add_question(set_id, q, a, b, c_, d, correct, idx=i)

    flash('Question set updated successfully!', 'success')
    return redirect(url_for('home'))

@app.route('/delete_quiz/<set_id>')
def delete_quiz(set_id):
    delete_set(set_id)
    flash('Question set deleted successfully!', 'success')
    return redirect(url_for('home'))

@app.route('/start_quiz/<set_id>')
def start_quiz(set_id):
    global active_quiz, current_question_index, student_scores, current_set_id
    questions = get_questions(set_id)
    if not questions:
        flash('This question set is empty. Please add questions before starting the quiz.', 'error')
        return redirect(url_for('edit_quiz', set_id=set_id))
    active_quiz = questions
    current_question_index = 0
    student_scores = defaultdict(int)
    current_set_id = set_id
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
def dashboard():
    if not active_quiz or current_question_index == -1 or current_question_index >= len(active_quiz):
        return redirect(url_for('home'))
    q = active_quiz[current_question_index]
    return render_template('dashboard.html',
                           quiz=q, index=current_question_index,
                           total=len(active_quiz), current_set_id=current_set_id)

@app.route('/next')
def next_question():
    global current_question_index
    if current_question_index < len(active_quiz) - 1:
        current_question_index += 1
        socketio.emit("new_question", active_quiz[current_question_index])
    return redirect(url_for('dashboard'))

@app.route('/receive_data')
def receive_data():
    student = (request.args.get('student') or '').strip()
    option = (request.args.get('option') or '').strip().upper()
    if student and 0 <= current_question_index < len(active_quiz):
        question = active_quiz[current_question_index]
        # avoid duplicate name in any option list
        if student not in sum(question['responses'].values(), []):
            question['responses'][option].append(student)
            correct = option == question['correct']
            if correct:
                student_scores[student] += 1
            # Persist response
            record_response(current_set_id, question['id'], student, option, correct)
            # Emit live response with embedded data
            response_data = {
                "student": student, 
                "option": option, 
                "correct": correct,
                "timestamp": datetime.now().isoformat(),
                "question_id": question['id'],
                "question_text": question['question']
            }
            socketio.emit('new_response', response_data)
            # Return JSON response with embedded live response data
            return jsonify(response_data), 200
    return jsonify({"error": "Invalid request"}), 400

@app.route('/analysis')
def analysis():
    if not current_set_id:
        return redirect(url_for('home'))

    set_details = question_set_details(current_set_id)
    total_questions = count_questions(current_set_id)

    student_performance = {}
    for student in student_scores:
        correct = correct_count_for_student_in_set(student, current_set_id)
        student_performance[student] = {
            'score': correct,
            'total': total_questions,
            'percentage': (correct / total_questions) * 100 if total_questions else 0
        }

    q_analysis = question_analysis_data(current_set_id)
    
    # Calculate overall statistics
    total_students = len(student_scores)
    total_responses = sum(len(question['responses'][opt]) for question in active_quiz for opt in ['A', 'B', 'C', 'D'])
    average_score = sum(perf['score'] for perf in student_performance.values()) / max(total_students, 1)
    average_percentage = sum(perf['percentage'] for perf in student_performance.values()) / max(total_students, 1)
    
    # Calculate question difficulty (lower percentage = more difficult)
    question_difficulty = []
    for q in q_analysis:
        if q[3] > 0:  # if there are responses
            difficulty_percentage = (q[4] / q[3]) * 100
            question_difficulty.append({
                'question': q[1],
                'difficulty': difficulty_percentage,
                'total_responses': q[3],
                'correct_responses': q[4]
            })

    return render_template('analysis.html',
                           set_details=set_details,
                           scores=sorted(student_scores.items(), key=lambda x: x[1], reverse=True),
                           performance=student_performance,
                           question_analysis=q_analysis,
                           total_students=total_students,
                           total_responses=total_responses,
                           average_score=average_score,
                           average_percentage=average_percentage,
                           question_difficulty=question_difficulty)

if __name__ == '__main__':
    # Local dev: threading backend; Render uses Gunicorn
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)

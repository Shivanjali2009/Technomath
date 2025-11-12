import os, json, base64
from datetime import datetime
from collections import defaultdict

from flask import Flask, request, render_template, redirect, url_for, flash, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

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


# ------------------ Flask App Configuration ------------------
app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')

# Respect Render/Cloudflare proxy headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


# ================== STORAGE HELPERS (Firestore) ==================

def create_question_set(name, class_name, level):
    """Create a new question set in Firestore."""
    db = get_db()
    doc_ref = db.collection("question_sets").document()
    doc_ref.set({
        "name": name,
        "class": class_name,
        "level": level,
        "created_at": firestore.SERVER_TIMESTAMP
    })
    return doc_ref.id

def list_question_sets():
    """Get all question sets ordered by creation date."""
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
    """Add a question to a question set."""
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
    """Get all questions for a question set."""
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
    """Delete all questions in a question set."""
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
    """Delete a question set and all its questions."""
    reset_questions(set_id)
    db = get_db()
    db.collection("question_sets").document(set_id).delete()

def record_response(set_id, question_id, student, option, is_correct):
    """Record a student's response in Firestore."""
    db = get_db()
    db.collection("responses").add({
        "set_id": set_id,
        "question_id": question_id,
        "student": student,
        "answer": option,
        "is_correct": is_correct,
        "timestamp": firestore.SERVER_TIMESTAMP
    })

def get_or_create_student(tag_id):
    """Get existing student or create new one with auto-incrementing number."""
    db = get_db()
    
    # First, try to find existing student by tag_id
    students_ref = db.collection("students")
    existing_student = students_ref.where("tag_id", "==", tag_id).limit(1).stream()
    
    for doc in existing_student:
        return doc.to_dict()["name"]
    
    # If not found, create new student
    # Get the next student number
    all_students = students_ref.order_by("created_at", direction=firestore.Query.DESCENDING).limit(1).stream()
    next_num = 1
    
    for doc in all_students:
        student_data = doc.to_dict()
        if "name" in student_data and student_data["name"].startswith("Student "):
            try:
                current_num = int(student_data["name"].split(" ")[1])
                next_num = current_num + 1
            except (ValueError, IndexError):
                pass
        break
    
    # Create new student
    student_name = f"Student {next_num:02d}"
    student_data = {
        "name": student_name,
        "tag_id": tag_id,
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP
    }
    
    # Add to Firestore
    doc_ref = db.collection("students").add(student_data)
    print(f"Created new student: {student_name} with tag_id: {tag_id}")
    
    return student_name

def get_all_students():
    """Get all students from Firestore."""
    db = get_db()
    students = []
    for doc in db.collection("students").order_by("created_at").stream():
        student_data = doc.to_dict()
        student_data["id"] = doc.id
        students.append(student_data)
    return students

def update_student_name(student_id, new_name):
    """Update student name."""
    db = get_db()
    db.collection("students").document(student_id).update({
        "name": new_name,
        "updated_at": firestore.SERVER_TIMESTAMP
    })

def delete_student(student_id):
    """Delete a student from Firestore."""
    db = get_db()
    db.collection("students").document(student_id).delete()

def delete_all_students():
    """Delete all students from Firestore."""
    db = get_db()
    students_ref = db.collection("students")
    batch = db.batch()
    count = 0
    for doc in students_ref.stream():
        batch.delete(doc.reference)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    return count

def create_student_from_name(student_name):
    """Create a student from a provided name (for manual API calls)."""
    db = get_db()
    
    # Check if student already exists with this name
    existing_student = db.collection("students").where("name", "==", student_name).limit(1).stream()
    
    for doc in existing_student:
        print(f"Found existing student: {student_name}")
        return student_name
    
    # If not found, create new student
    # Generate a unique tag_id for manual entries
    import uuid
    tag_id = f"manual_{str(uuid.uuid4())[:8]}"
    
    student_data = {
        "name": student_name,
        "tag_id": tag_id,
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP
    }
    
    # Add to Firestore
    doc_ref = db.collection("students").add(student_data)
    print(f"Created new student from name: {student_name} with tag_id: {tag_id}")
    
    return student_name

def question_set_details(set_id):
    """Get details of a question set."""
    db = get_db()
    doc = db.collection("question_sets").document(set_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict() or {}
    return (d.get("name"), d.get("class"), d.get("level"))

def count_questions(set_id):
    """Count questions in a question set."""
    db = get_db()
    return sum(1 for _ in db.collection("question_sets").document(set_id)
                 .collection("questions").stream())

def correct_count_for_student_in_set(student, set_id):
    """Count correct responses for a student in a set."""
    db = get_db()
    snaps = db.collection("responses")\
              .where("student", "==", student)\
              .where("set_id", "==", set_id)\
              .where("is_correct", "==", True)\
              .stream()
    return sum(1 for _ in snaps)

def question_analysis_data(set_id):
    """Get analysis data for all questions in a set."""
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


# ================== SESSION MANAGEMENT ==================
quiz_sessions = {}
current_session_id = None

def create_quiz_session(set_id, questions):
    """Create a new quiz session."""
    return {
        'active_quiz': questions,
        'current_question_index': 0,
        'student_scores': defaultdict(int),
        'current_set_id': set_id,
        'created_at': datetime.now()
    }

def cleanup_expired_sessions():
    """Clean up sessions older than 24 hours."""
    current_time = datetime.now()
    expired_sessions = []
    
    for session_id, session_data in quiz_sessions.items():
        session_age = current_time - session_data['created_at']
        if session_age.total_seconds() > 86400:  # 24 hours
            expired_sessions.append(session_id)
    
    for session_id in expired_sessions:
        del quiz_sessions[session_id]
        print(f"Cleaned up expired session: {session_id}")
    
    return len(expired_sessions)


# ================== ROUTES ==================

def get_question_sets():
    """Get all question sets."""
    return list_question_sets()

@app.route('/healthz')
def healthz():
    """Health check endpoint."""
    return "ok", 200

@app.route('/api/debug')
def debug():
    """Debug endpoint to check server state."""
    return jsonify({
        "current_session_id": current_session_id,
        "active_sessions": list(quiz_sessions.keys()),
        "current_session": quiz_sessions.get(current_session_id, {}) if current_session_id else {}
    })

@app.route('/__firetest')
def firetest():
    """Test Firebase connection."""
    try:
        db = get_db()
        next(db.collection("question_sets").limit(1).stream(), None)
        return "firestore ok", 200
    except Exception as e:
        return f"firestore error: {e}", 500

@app.route('/')
def home():
    """Home page with question sets."""
    question_sets = get_question_sets()
    return render_template('index.html', question_sets=question_sets)

@app.route('/create_question_set', methods=['POST'])
def create_question_set_route():
    """Create a new question set."""
    name = request.form.get('name')
    class_name = request.form.get('class')
    level = request.form.get('level')
    set_id = create_question_set(name, class_name, level)
    return redirect(url_for('upload_questions', set_id=set_id))

@app.route('/upload_questions/<set_id>')
def upload_questions(set_id):
    """Upload questions page."""
    return render_template('upload.html', set_id=set_id)

@app.route('/save_questions/<set_id>', methods=['POST'])
def save_questions(set_id):
    """Save questions to a question set."""
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
    """Edit quiz page."""
    qs = question_set_details(set_id)
    questions = get_questions(set_id)
    if not qs:
        flash('Question set not found.', 'error')
        return redirect(url_for('home'))
    question_set = (set_id, qs[0], qs[1], qs[2], None)
    return render_template('edit_quiz.html', question_set=question_set, questions=questions)

@app.route('/update_quiz/<set_id>', methods=['POST'])
def update_quiz(set_id):
    """Update a quiz."""
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
    """Delete a quiz."""
    delete_set(set_id)
    flash('Question set deleted successfully!', 'success')
    return redirect(url_for('home'))

@app.route('/start_quiz/<set_id>')
def start_quiz(set_id):
    """Start a quiz session."""
    global current_session_id
    questions = get_questions(set_id)
    if not questions:
        flash('This question set is empty. Please add questions before starting the quiz.', 'error')
        return redirect(url_for('edit_quiz', set_id=set_id))
    
    # Create a new session
    session_id = f"quiz_{set_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    quiz_sessions[session_id] = create_quiz_session(set_id, questions)
    current_session_id = session_id
    
    print(f"Debug: Started quiz session {session_id} for set {set_id}")
    print(f"Debug: Total sessions: {len(quiz_sessions)}")
    
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
def dashboard():
    """Quiz dashboard."""
    # Clean up expired sessions first
    cleanup_expired_sessions()
    
    print(f"Debug: Dashboard access - current_session_id = {current_session_id}")
    print(f"Debug: Available sessions = {list(quiz_sessions.keys())}")
    
    if not current_session_id:
        print("Debug: No current session ID")
        return redirect(url_for('home'))
    
    if current_session_id not in quiz_sessions:
        print(f"Debug: Session {current_session_id} not found in active sessions")
        return redirect(url_for('home'))
    
    session = quiz_sessions[current_session_id]
    active_quiz = session['active_quiz']
    current_question_index = session['current_question_index']
    
    print(f"Debug: Session found - question_index = {current_question_index}, quiz_length = {len(active_quiz)}")
    
    if current_question_index < 0 or current_question_index >= len(active_quiz):
        print("Debug: Invalid question index")
        return redirect(url_for('home'))
    
    q = active_quiz[current_question_index]
    return render_template('dashboard.html',
                           quiz=q, index=current_question_index,
                           total=len(active_quiz), current_set_id=session['current_set_id'],
                           session_id=current_session_id)

@app.route('/next')
def next_question():
    """Move to next question."""
    if not current_session_id or current_session_id not in quiz_sessions:
        return redirect(url_for('home'))
    
    session = quiz_sessions[current_session_id]
    active_quiz = session['active_quiz']
    
    if session['current_question_index'] < len(active_quiz) - 1:
        session['current_question_index'] += 1
    
    return redirect(url_for('dashboard'))

@app.route('/receive_data')
def receive_data():
    """Receive student response data."""
    try:
        # Get raw parameters for debugging
        raw_tag_id = request.args.get('tag_id', '')
        raw_student = request.args.get('student', '')
        raw_option = request.args.get('option', '')
        
        print(f"🔍 Raw parameters received:")
        print(f"  - tag_id: '{raw_tag_id}'")
        print(f"  - student: '{raw_student}'")
        print(f"  - option: '{raw_option}'")
        
        # Process parameters
        tag_id = raw_tag_id.strip()
        student_name = raw_student.strip()
        option = raw_option.strip().upper()
        
        print(f"🔍 Processed parameters:")
        print(f"  - tag_id: '{tag_id}'")
        print(f"  - student_name: '{student_name}'")
        print(f"  - option: '{option}'")
        
        # If tag_id is provided, use it to get/create student
        if tag_id:
            student = get_or_create_student(tag_id)
            print(f"👤 Student from tag_id: '{student}'")
        elif student_name:
            # If student name is provided, create a student with that name
            student = create_student_from_name(student_name)
            print(f"👤 Student from name: '{student}'")
        else:
            print("❌ No tag_id or student name provided")
            return jsonify({"error": "Missing tag_id or student name"}), 400
        
        if not option:
            print("❌ No option provided")
            return jsonify({"error": "Missing option"}), 400
        
        # Check if there's an active quiz session
        if not current_session_id or current_session_id not in quiz_sessions:
            print(f"❌ No active session. current_session_id: {current_session_id}")
            return jsonify({"error": "No active quiz session. Please start a quiz first by visiting /start_quiz/<set_id>"}), 400
        
        session = quiz_sessions[current_session_id]
        active_quiz = session['active_quiz']
        current_question_index = session['current_question_index']
        
        print(f"📊 Session info:")
        print(f"  - current_question_index: {current_question_index}")
        print(f"  - quiz_length: {len(active_quiz)}")
        
        if current_question_index < 0 or current_question_index >= len(active_quiz):
            print("❌ Invalid question index")
            return jsonify({"error": "Invalid question index"}), 400
        
        question = active_quiz[current_question_index]
        
        print(f"📝 Question info:")
        print(f"  - question_id: {question['id']}")
        print(f"  - question_text: {question['question']}")
        print(f"  - correct_answer: {question['correct']}")
        print(f"  - available_options: {list(question['options'].keys())}")
        
        # Check for duplicate responses
        all_responses = sum(question['responses'].values(), [])
        if student in all_responses:
            print(f"⚠️ Student '{student}' already responded to this question")
            return jsonify({"error": "Student has already responded to this question"}), 400
        
        # Validate option
        print(f"🔍 Option validation:")
        print(f"  - received option: '{option}' (type: {type(option)})")
        print(f"  - question options: {question['options']}")
        print(f"  - option keys: {list(question['options'].keys())}")
        print(f"  - option in keys: {option in question['options']}")
        
        if option not in question['options']:
            print(f"❌ Invalid option '{option}'. Valid options: {list(question['options'].keys())}")
            return jsonify({"error": f"Invalid option '{option}'. Valid options are: {', '.join(question['options'].keys())}"}), 400
        
        # Add response to in-memory state
        question['responses'][option].append(student)
        correct = option == question['correct']
        
        print(f"✅ Response added:")
        print(f"  - student: '{student}'")
        print(f"  - option: '{option}'")
        print(f"  - correct: {correct}")
        print(f"  - correct_answer: '{question['correct']}'")
        
        if correct:
            session['student_scores'][student] += 1
            print(f"🎯 Student '{student}' got it right! Score: {session['student_scores'][student]}")
        
        # Persist response to Firestore
        record_response(session['current_set_id'], question['id'], student, option, correct)
        
        # Return JSON response with embedded live response data
        response_data = {
            "student": student, 
            "option": option, 
            "correct": correct,
            "timestamp": datetime.now().isoformat(),
            "question_id": question['id'],
            "question_text": question['question'],
            "tag_id": tag_id if tag_id else None
        }
        
        print(f"📤 Returning response: {response_data}")
        return jsonify(response_data), 200
        
    except Exception as e:
        print(f"❌ Error in receive_data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/live_responses')
def live_responses():
    """API endpoint for getting live responses from Firestore."""
    try:
        print(f"🔍 Debug: current_session_id = {current_session_id}")
        print(f"🔍 Debug: quiz_sessions keys = {list(quiz_sessions.keys())}")
        
        if not current_session_id:
            return jsonify({"error": "No active quiz session. Please start a quiz first."}), 400
        
        if current_session_id not in quiz_sessions:
            return jsonify({"error": "Session expired. Please start a new quiz."}), 400
        
        session = quiz_sessions[current_session_id]
        current_set_id = session['current_set_id']
        active_quiz = session['active_quiz']
        current_question_index = session['current_question_index']
        
        print(f"🔍 Debug: current_question_index = {current_question_index}, quiz_length = {len(active_quiz)}")
        
        if current_question_index < 0 or current_question_index >= len(active_quiz):
            return jsonify({"error": "Invalid question index"}), 400
        
        current_question = active_quiz[current_question_index]
        question_id = current_question['id']
        
        # Get responses directly from Firebase - ONLY for current question_id (persistent, single source of truth)
        print(f"🔍 Getting responses from Firebase for question_id: {question_id}")
        db = get_db()
        
        responses = []
        
        # Query Firebase for responses to THIS SPECIFIC question only
        try:
            firestore_responses = db.collection("responses")\
                .where("question_id", "==", question_id)\
                .stream()
            
            print(f"📊 Querying Firebase for question_id: {question_id}")
            response_count = 0
            seen_students = {}  # Track latest response per student: {student: timestamp}
            
            for doc in firestore_responses:
                response_count += 1
                resp_data = doc.to_dict()
                student = resp_data.get("student", "")
                option = resp_data.get("answer", "")
                is_correct = resp_data.get("is_correct", False)
                timestamp = resp_data.get("timestamp")
                
                # Skip if missing required fields
                if not student or not option:
                    print(f"⚠️ Skipping response with missing data: student={student}, option={option}")
                    continue
                
                # CRITICAL: Only include if question_id matches (double check)
                resp_question_id = resp_data.get("question_id", "")
                if resp_question_id != question_id:
                    print(f"⚠️ Skipping response with wrong question_id: {resp_question_id} (expected: {question_id})")
                    continue
                
                # Convert Firestore timestamp to ISO format
                timestamp_str = datetime.now().isoformat()  # Default
                if timestamp:
                    try:
                        # Firestore Timestamp object
                        if hasattr(timestamp, 'timestamp'):
                            # It's a Firestore Timestamp
                            dt = datetime.fromtimestamp(timestamp.timestamp())
                            timestamp_str = dt.isoformat()
                        elif isinstance(timestamp, datetime):
                            timestamp_str = timestamp.isoformat()
                        elif hasattr(timestamp, 'isoformat'):
                            timestamp_str = timestamp.isoformat()
                        else:
                            # Try to convert string or other format
                            timestamp_str = str(timestamp)
                    except Exception as e:
                        print(f"⚠️ Error converting timestamp: {e}, using current time")
                        timestamp_str = datetime.now().isoformat()
                
                # Track latest response per student (keep only the most recent)
                if student in seen_students:
                    # Compare timestamps to keep the latest
                    if timestamp_str > seen_students[student]['timestamp']:
                        # Remove old response and add new one
                        responses = [r for r in responses if r['student'] != student]
                        seen_students[student] = {'timestamp': timestamp_str}
                    else:
                        # Skip this older response
                        print(f"⚠️ Skipping older duplicate response for student: {student}")
                        continue
                else:
                    seen_students[student] = {'timestamp': timestamp_str}
                
                response_data = {
                    "student": student,
                    "option": option,
                    "correct": is_correct,
                    "timestamp": timestamp_str,
                    "question_id": question_id,
                    "question_text": current_question['question'],
                    "response_id": f"{student}_{option}_{question_id}_{timestamp_str}"
                }
                responses.append(response_data)
                print(f"  ✅ Added response: {student} -> {option} ({'✅' if is_correct else '❌'}) at {timestamp_str}")
            
            print(f"📊 Found {response_count} total responses in Firebase, processed {len(responses)} unique responses")
            
        except Exception as e:
            print(f"❌ Error querying Firebase: {e}")
            import traceback
            traceback.print_exc()
            # Fallback: try to get from in-memory state
            print("⚠️ Falling back to in-memory state")
            for option, students in current_question['responses'].items():
                for student in students:
                    correct = option == current_question['correct']
                    response_data = {
                        "student": student,
                        "option": option,
                        "correct": correct,
                        "timestamp": datetime.now().isoformat(),
                        "question_id": question_id,
                        "question_text": current_question['question'],
                        "response_id": f"{student}_{option}_{question_id}"
                    }
                    responses.append(response_data)
        
        # Sort by timestamp (most recent first)
        try:
            responses.sort(key=lambda x: x['timestamp'], reverse=True)
        except Exception as e:
            print(f"⚠️ Error sorting responses: {e}")
        
        print(f"📊 Live responses for question {current_question_index + 1}: {len(responses)} responses")
        if len(responses) > 0:
            for resp in responses:
                print(f"  - {resp['student']}: {resp['option']} ({'✅' if resp['correct'] else '❌'}) at {resp['timestamp']}")
        else:
            print("  ⚠️ No responses found for this question")
        
        # Always return a valid response, even if empty
        return jsonify({
            "question_id": question_id,
            "question_text": current_question['question'],
            "responses": responses if responses else [],  # Ensure it's always a list
            "session_id": current_session_id,
            "question_index": current_question_index,
            "total_responses": len(responses)
        })
    except Exception as e:
        print(f"❌ Error in live_responses: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/students')
def students_page():
    """Students management page."""
    students = get_all_students()
    return render_template('students.html', students=students)

@app.route('/api/students')
def api_students():
    """API endpoint to get all students."""
    students = get_all_students()
    return jsonify(students)

@app.route('/api/update_student', methods=['POST'])
def api_update_student():
    """API endpoint to update student name."""
    data = request.get_json()
    student_id = data.get('id')
    new_name = data.get('name', '').strip()
    
    if not student_id or not new_name:
        return jsonify({"error": "Missing student ID or name"}), 400
    
    try:
        update_student_name(student_id, new_name)
        return jsonify({"success": True, "message": "Student name updated successfully"})
    except Exception as e:
        return jsonify({"error": f"Failed to update student: {str(e)}"}), 500

@app.route('/api/delete_student', methods=['POST'])
def api_delete_student():
    """API endpoint to delete a student."""
    data = request.get_json()
    student_id = data.get('id')
    
    if not student_id:
        return jsonify({"error": "Missing student ID"}), 400
    
    try:
        delete_student(student_id)
        return jsonify({"success": True, "message": "Student deleted successfully"})
    except Exception as e:
        return jsonify({"error": f"Failed to delete student: {str(e)}"}), 500

@app.route('/api/delete_all_students', methods=['POST'])
def api_delete_all_students():
    """API endpoint to delete all students."""
    try:
        count = delete_all_students()
        return jsonify({"success": True, "message": f"Successfully deleted {count} students"})
    except Exception as e:
        return jsonify({"error": f"Failed to delete all students: {str(e)}"}), 500

@app.route('/analysis')
def analysis():
    """Quiz analysis page."""
    if not current_session_id or current_session_id not in quiz_sessions:
        return redirect(url_for('home'))

    session = quiz_sessions[current_session_id]
    current_set_id = session['current_set_id']
    student_scores = session['student_scores']
    active_quiz = session['active_quiz']

    set_details = question_set_details(current_set_id)
    total_questions = count_questions(current_set_id)

    # Get all students who have responded from Firebase (not just in-memory session)
    db = get_db()
    all_responses = db.collection("responses")\
        .where("set_id", "==", current_set_id)\
        .stream()
    
    # Collect all unique students from Firebase responses
    students_from_responses = set()
    for doc in all_responses:
        resp_data = doc.to_dict()
        student = resp_data.get("student", "")
        if student:
            students_from_responses.add(student)
    
    # Combine students from session and Firebase
    all_students = set(student_scores.keys()) | students_from_responses
    
    student_performance = {}
    for student in all_students:
        correct = correct_count_for_student_in_set(student, current_set_id)
        student_performance[student] = {
            'score': correct,
            'total': total_questions,
            'percentage': (correct / total_questions) * 100 if total_questions else 0
        }
    
    # Debug: Print performance data
    print(f"📊 Student performance data: {len(student_performance)} students")
    for student, perf in student_performance.items():
        print(f"  - {student}: score={perf['score']}, total={perf['total']}, percentage={perf['percentage']:.1f}%")

    q_analysis = question_analysis_data(current_set_id)
    
    # Calculate overall statistics
    total_students = len(all_students)  # Use combined students from session and Firebase
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
    # Local dev: standard Flask run; Render uses Gunicorn
    app.run(host='0.0.0.0', port=5000, debug=True)
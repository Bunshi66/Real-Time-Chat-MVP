import os
from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, join_room, leave_room, emit, disconnect
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta

app = Flask(__name__)

basedir = os.path.abspath(os.path.dirname(__file__))

app.config['SECRET_KEY'] = 'secret!'
db_path = os.path.join(basedir, 'data', 'chat.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
# allow_unsafe_werkzeug нужен для dev-сервера, если используешь последние версии
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

def get_msk_time():
    return datetime.utcnow() + timedelta(hours=3)

# --- МОДЕЛИ (Без изменений) ---
user_rooms = db.Table('user_rooms',
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
                      db.Column('room_id', db.Integer, db.ForeignKey('room.id'), primary_key=True)
                      )


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    status = db.Column(db.String(20), default='offline')
    last_seen = db.Column(db.DateTime, default=get_msk_time)
    rooms = db.relationship('Room', secondary=user_rooms, lazy='subquery',
                            backref=db.backref('users', lazy=True))


class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_name = db.Column(db.String(50), nullable=False)
    sender = db.Column(db.String(80), nullable=False)
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=get_msk_time)


with app.app_context():
    if not os.path.exists(os.path.join(basedir, 'data')):
        os.makedirs(os.path.join(basedir, 'data'), exist_ok=True)

    try:
        test_file = os.path.join(basedir, 'data', 'perm_test.txt')
        with open(test_file, 'w') as f:
            f.write('write test')
        print(f"--- SUCCESS: Write permission to {test_file} OK ---")
    except Exception as e:
        print(f"--- ERROR: Cannot write to data folder: {e} ---")

    db.create_all()
@app.route('/')
def index():
    return render_template('index.html')



# --- СОБЫТИЯ ---

@socketio.on('login')
def on_login(data):
    username = data['username']
    session['username'] = username  # Сохраняем в сессию сокета

    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(username=username)
        db.session.add(user)

    user.status = 'online'
    db.session.commit()

    my_rooms = [{'name': r.name} for r in user.rooms]
    emit('login_response', {'success': True, 'rooms': my_rooms})


@socketio.on('join_room_event')
def on_join_room(data):
    username = data['username']
    room_name = data['room']

    session['room'] = room_name
    session['username'] = username

    user = User.query.filter_by(username=username).first()
    room = Room.query.filter_by(name=room_name).first()

    # 1. Создание комнаты
    if not room:
        room = Room(name=room_name)
        db.session.add(room)
        db.session.commit()

    # 2. Привязка пользователя
    if user not in room.users:
        room.users.append(user)

    user.status = 'online'
    db.session.commit()

    join_room(room_name)

    # 3. Обновляем список комнат у пользователя (чтобы новая комната появилась в меню)
    my_rooms = [{'name': r.name} for r in user.rooms]
    emit('update_room_list', {'rooms': my_rooms})

    # 4. История сообщений
    messages = Message.query.filter_by(room_name=room_name).order_by(Message.timestamp.asc()).limit(50).all()
    history = [{'sender': m.sender, 'text': m.text, 'time': m.timestamp.strftime('%H:%M')} for m in messages]
    emit('load_history', history)

    # 5. Список участников
    participants = []
    for u in room.users:
        # Формат времени для last seen
        last_seen_str = u.last_seen.strftime('%d.%m %H:%M') if u.last_seen else ""
        participants.append({
            'username': u.username,
            'status': u.status,
            'last_seen': last_seen_str
        })
    emit('room_info', {'participants': participants})

    # 6. Уведомляем всех в комнате, что этот юзер стал ONLINE
    # Здесь можно использовать обычный emit, так как сокет жив
    socketio.emit('user_status_change', {
        'username': username,
        'status': 'online',
        'last_seen': None
    }, to=room_name)


@socketio.on('send_message_event')
def handle_message(data):
    username = data['username']
    room_name = data['room']
    text = data['message']

    new_msg = Message(sender=username, room_name=room_name, text=text)
    db.session.add(new_msg)
    db.session.commit()

    emit('receive_message', {
        'sender': username,
        'text': text,
        'time': new_msg.timestamp.strftime('%H:%M')
    }, to=room_name)


@socketio.on('leave_room_event')
def on_leave_room(data):
    username = data.get('username')
    room_name = data.get('room')
    if username and room_name:
        leave_room(room_name)
        # Мы не меняем статус на offline, так как он просто вышел в меню
        # Но можно уведомить, если нужно (сейчас не будем спамить)


# --- [ГЛАВНОЕ ИСПРАВЛЕНИЕ ЗДЕСЬ] ---
@socketio.on('disconnect')
def on_disconnect():
    username = session.get('username')

    if username:
        user = User.query.filter_by(username=username).first()
        if user:
            user.status = 'offline'
            user.last_seen = datetime.utcnow()
            db.session.commit()

            last_seen_str = user.last_seen.strftime('%d.%m %H:%M')

            # ВАЖНО: Используем socketio.emit (глобальный), а не flask_socketio.emit
            # Так как контекст текущего соединения разрывается.
            for r in user.rooms:
                socketio.emit('user_status_change', {
                    'username': username,
                    'status': 'offline',
                    'last_seen': last_seen_str
                }, to=r.name)


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
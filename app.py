from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, join_room, leave_room, emit, disconnect
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# --- МОДЕЛИ БД (Без изменений) ---
user_rooms = db.Table('user_rooms',
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
                      db.Column('room_id', db.Integer, db.ForeignKey('room.id'), primary_key=True)
                      )


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    status = db.Column(db.String(20), default='offline')
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
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
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


with app.app_context():
    db.create_all()


@app.route('/')
def index():
    return render_template('index.html')


# --- СОБЫТИЯ ---

@socketio.on('login')
def on_login(data):
    username = data['username']
    session['username'] = username

    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(username=username)
        db.session.add(user)

    user.status = 'online'
    db.session.commit()

    # Возвращаем список комнат, где пользователь уже является участником
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

    # 1. Создаем комнату, если нет
    if not room:
        room = Room(name=room_name)
        db.session.add(room)
        db.session.commit()  # Важно сохранить комнату, чтобы у нее появился ID

    # 2. Добавляем пользователя в участники комнаты (Навсегда)
    # Если юзера еще нет в списке users этой комнаты, добавляем
    if user not in room.users:
        room.users.append(user)

    # Обновляем статус текущего юзера
    user.status = 'online'
    db.session.commit()  # Сохраняем все связи

    # Подключаем сокет к каналу
    join_room(room_name)

    # 3. Загружаем историю
    messages = Message.query.filter_by(room_name=room_name).order_by(Message.timestamp.asc()).limit(50).all()
    history = [{'sender': m.sender, 'text': m.text, 'time': m.timestamp.strftime('%H:%M')} for m in messages]
    emit('load_history', history)

    # 4. Формируем ПОЛНЫЙ список участников (и Online, и Offline)
    # Благодаря связи room.users мы получим всех, кто когда-либо заходил
    participants = []
    for u in room.users:
        last_seen_str = u.last_seen.strftime('%d.%m %H:%M') if u.last_seen else "Давно"
        participants.append({
            'username': u.username,
            'status': u.status,  # 'online' или 'offline' из БД
            'last_seen': last_seen_str
        })

    # Отправляем список ТОЛЬКО тому, кто зашел (чтобы отрисовать интерфейс)
    emit('room_info', {'participants': participants})

    # 5. Уведомляем ОСТАЛЬНЫХ, что статус этого юзера изменился на Online
    # Используем broadcast=True (по умолчанию для to=...), но skip_sid не нужен,
    # так как мы хотим, чтобы у отправителя тоже обновился статус (хотя room_info это уже сделал)
    emit('user_status_change', {
        'username': username,
        'status': 'online',
        'last_seen': None
    }, to=room_name, include_self=False)


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

            # Уведомляем ВСЕ комнаты, в которых состоит юзер, что он вышел
            for r in user.rooms:
                emit('user_status_change', {
                    'username': username,
                    'status': 'offline',
                    'last_seen': last_seen_str
                }, to=r.name)

@socketio.on('send_message_event')
def handle_message(data):
    username = data['username']
    room_name = data['room']
    text = data['message']

    # Сохраняем в БД
    new_msg = Message(sender=username, room_name=room_name, text=text)
    db.session.add(new_msg)
    db.session.commit()

    # Отправляем всем в комнате (включая отправителя)
    emit('receive_message', {
        'sender': username,
        'text': text,
        'time': new_msg.timestamp.strftime('%H:%M')
    }, to=room_name)


# --- [ИСПРАВЛЕНИЕ 2] Выход из комнаты (назад в лобби) ---
@socketio.on('leave_room_event')
def on_leave_room(data):
    username = data.get('username')
    room_name = data.get('room')

    if username and room_name:
        leave_room(room_name)
        # Опционально: можно не ставить offline, если он просто вышел в меню
        # Но для примера оставим его online, просто уведомим комнату, что он вышел
        emit('user_left_room', {'username': username}, to=room_name)


# --- [ИСПРАВЛЕНИЕ 3] Настоящий дисконнект (закрытие вкладки) ---
@socketio.on('disconnect')
def on_disconnect():
    # Мы берем username из сессии, так как disconnect не принимает аргументов data
    username = session.get('username')

    if username:
        user = User.query.filter_by(username=username).first()
        if user:
            user.status = 'offline'
            user.last_seen = datetime.utcnow()
            db.session.commit()

            # Уведомляем все комнаты, где был этот юзер
            for r in user.rooms:
                emit('user_disconnected', {
                    'username': username,
                    'last_seen': user.last_seen.strftime('%H:%M')
                }, to=r.name)

        # Очищаем сессию (опционально, SocketIO сам чистит свой контекст)
        # session.pop('username', None)


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
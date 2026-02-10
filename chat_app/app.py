from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Инициализация
db = SQLAlchemy(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")


# --- Модели БД ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    status = db.Column(db.String(20), default='offline')  # online / offline
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(50), nullable=False)
    sender = db.Column(db.String(80), nullable=False)
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


# Создаем таблицы при запуске (для MVP так можно)
with app.app_context():
    db.create_all()


# --- Маршруты ---
@app.route('/')
def index():
    return render_template('index.html')


# --- События WebSocket ---

@socketio.on('join')
def on_join(data):
    username = data['username']
    room = data['room']

    # Обновляем или создаем юзера
    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(username=username)
        db.session.add(user)

    user.status = 'online'
    db.session.commit()

    join_room(room)

    # Отправляем историю сообщений (последние 20)
    messages = Message.query.filter_by(room=room).order_by(Message.timestamp.asc()).limit(50).all()
    history = [{'sender': m.sender, 'text': m.text, 'time': m.timestamp.strftime('%H:%M')} for m in messages]
    emit('load_history', history)

    # Уведомляем комнату
    emit('status_update', {'username': username, 'status': 'online'}, to=room)


@socketio.on('send_message')
def on_send(data):
    room = data['room']
    sender = data['username']
    text = data['message']

    # Сохраняем в БД
    msg = Message(room=room, sender=sender, text=text)
    db.session.add(msg)
    db.session.commit()

    # Рассылаем всем в комнате
    emit('receive_message', {
        'sender': sender,
        'text': text,
        'time': datetime.utcnow().strftime('%H:%M')
    }, to=room)


@socketio.on('disconnect_request')
def on_disconnect_request(data):
    username = data.get('username')
    if username:
        user = User.query.filter_by(username=username).first()
        if user:
            user.status = 'offline'
            user.last_seen = datetime.utcnow()
            db.session.commit()
            emit('status_update', {'username': username, 'status': 'offline'}, broadcast=True)


if __name__ == '__main__':
    # host='0.0.0.0' важен, чтобы приложение было доступно извне контейнера/сети
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
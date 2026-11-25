import time
import uuid
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ghost-vault-super-secret-key'

# 25 MB buffer taaki har type ki file (PDF, Video, Photo, ZIP) smoothly jaye
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    max_http_buffer_size=25 * 1024 * 1024
)

# Pure RAM storage (Zero database)
rooms = {}
sid_to_room = {}

PENDING_TTL = 600  # Offline RAM text max 10 min tak rahega, padhte hi delete


def cleanup_expired_pending(room_id):
    if room_id in rooms:
        now = time.time()
        rooms[room_id]['pending'] = [
            m for m in rooms[room_id]['pending'] if now - m['ts'] < PENDING_TTL
        ]


def broadcast_room_status(room_id):
    if room_id not in rooms:
        return
    users = rooms[room_id]['users']
    user_list = [{'sid': s, 'name': n} for s, n in users.items()]
    emit('room_status', {
        'count': len(users),
        'users': user_list
    }, to=room_id)


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('join_vault')
def handle_join(data):
    room_id = str(data.get('room', '')).strip().lower()
    pin = str(data.get('pin', '')).strip()
    name = str(data.get('name', 'Partner')).strip() or 'Partner'
    sid = request.sid

    if not room_id or len(pin) < 4:
        emit('join_error', {'msg': 'Room name aur kam se kam 4-digit PIN daalo!'})
        return

    if room_id not in rooms:
        rooms[room_id] = {
            'pin': pin,
            'users': {},
            'pending': []
        }

    room = rooms[room_id]

    if room['pin'] != pin:
        emit('join_error', {'msg': 'Galat PIN hai! Sahi secret PIN enter karo.'})
        return

    if len(room['users']) >= 2 and sid not in room['users']:
        emit('join_error', {'msg': 'Room Locked hai! Is room mein already 2 log baat kar rahe hain.'})
        return

    join_room(room_id)
    room['users'][sid] = name
    sid_to_room[sid] = room_id

    cleanup_expired_pending(room_id)

    emit('join_success', {
        'room': room_id,
        'mySid': sid,
        'myName': name
    })

    broadcast_room_status(room_id)

    # Pending RAM messages deliver karke turant wipe karo
    if room['pending']:
        delivered = []
        remaining = []
        for m in room['pending']:
            if m['sender_sid'] != sid:
                delivered.append(m)
            else:
                remaining.append(m)
        if delivered:
            for msg in delivered:
                emit('receive_message', {
                    'id': msg['id'],
                    'text': msg['text'],
                    'sender': msg['sender'],
                    'senderSid': msg['sender_sid'],
                    'fromRam': True,
                    'autoDestruct': msg.get('autoDestruct', False),
                    'oneTime': False,
                    'edited': msg.get('edited', False)
                })
            room['pending'] = remaining
            emit('system_note', {'msg': 'Temporary RAM message deliver hoke server se delete ho gaya.'}, to=room_id)


@socketio.on('send_message')
def handle_message(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if not room_id or room_id not in rooms:
        return

    text = str(data.get('text', '')).strip()
    auto_destruct = bool(data.get('autoDestruct', False))
    one_time = bool(data.get('oneTime', False))
    if not text:
        return

    room = rooms[room_id]
    sender_name = room['users'].get(sid, 'Partner')
    msg_id = uuid.uuid4().hex[:10]

    if one_time and len(room['users']) < 2:
        emit('system_note', {'msg': 'Partner offline hai! One-Time message sirf live hone par bhej sakte ho.'})
        return

    if len(room['users']) >= 2:
        emit('receive_message', {
            'id': msg_id,
            'text': text,
            'sender': sender_name,
            'senderSid': sid,
            'fromRam': False,
            'autoDestruct': auto_destruct,
            'oneTime': one_time,
            'edited': False
        }, to=room_id)
    else:
        cleanup_expired_pending(room_id)
        room['pending'].append({
            'id': msg_id,
            'text': text,
            'sender': sender_name,
            'sender_sid': sid,
            'ts': time.time(),
            'autoDestruct': auto_destruct,
            'edited': False
        })
        emit('receive_message', {
            'id': msg_id,
            'text': text,
            'sender': sender_name,
            'senderSid': sid,
            'fromRam': True,
            'autoDestruct': auto_destruct,
            'oneTime': False,
            'edited': False
        })


@socketio.on('edit_message')
def handle_edit_message(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if not room_id or room_id not in rooms:
        return

    msg_id = str(data.get('msgId', '')).strip()
    new_text = str(data.get('newText', '')).strip()
    if not msg_id or not new_text:
        return

    # Agar message abhi RAM pending buffer mein hai toh wahan bhi update karo
    for m in rooms[room_id]['pending']:
        if m['id'] == msg_id and m['sender_sid'] == sid:
            m['text'] = new_text
            m['edited'] = True

    emit('message_edited', {
        'msgId': msg_id,
        'newText': new_text,
        'editorSid': sid
    }, to=room_id)


@socketio.on('send_file')
def handle_send_file(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if not room_id or room_id not in rooms:
        return

    file_data = data.get('fileData')
    file_name = str(data.get('fileName', 'file')).strip() or 'file'
    file_type = str(data.get('fileType', 'application/octet-stream')).strip()
    file_size = int(data.get('fileSize', 0))
    one_time = bool(data.get('oneTime', False))
    auto_destruct = bool(data.get('autoDestruct', False))

    if not file_data:
        return

    # Files RAM mein store nahi hongi — dono live hone par direct relay hongi
    if len(rooms[room_id]['users']) < 2:
        emit('system_note', {'msg': 'Partner offline hai! Photo ya file bhejne ke liye dono ka live hona zaroori hai.'})
        return

    sender_name = rooms[room_id]['users'].get(sid, 'Partner')
    file_id = uuid.uuid4().hex[:10]

    emit('receive_file', {
        'id': file_id,
        'fileData': file_data,
        'fileName': file_name,
        'fileType': file_type,
        'fileSize': file_size,
        'sender': sender_name,
        'senderSid': sid,
        'oneTime': one_time,
        'autoDestruct': auto_destruct
    }, to=room_id)


@socketio.on('typing')
def handle_typing(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id and room_id in rooms:
        emit('partner_typing', {'isTyping': bool(data.get('isTyping'))}, to=room_id, include_self=False)


@socketio.on('knock_partner')
def handle_knock():
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id and room_id in rooms:
        emit('knock_received', {'msg': 'Study Reminder: Physics notes update check kar lo!'}, to=room_id, include_self=False)


# --- WebRTC Voice & Video Call Signaling ---

@socketio.on('call_request')
def handle_call_request(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id and room_id in rooms:
        caller_name = rooms[room_id]['users'].get(sid, 'Partner')
        emit('incoming_call', {
            'callType': data.get('callType', 'video'),
            'callerName': caller_name
        }, to=room_id, include_self=False)


@socketio.on('call_accept')
def handle_call_accept(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id:
        emit('call_accepted', {'callType': data.get('callType', 'video')}, to=room_id, include_self=False)


@socketio.on('call_reject')
def handle_call_reject():
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id:
        emit('call_rejected', {}, to=room_id, include_self=False)


@socketio.on('call_end')
def handle_call_end():
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id:
        emit('call_ended', {}, to=room_id, include_self=False)


@socketio.on('webrtc_offer')
def handle_offer(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id:
        emit('webrtc_offer', data, to=room_id, include_self=False)


@socketio.on('webrtc_answer')
def handle_answer(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id:
        emit('webrtc_answer', data, to=room_id, include_self=False)


@socketio.on('webrtc_ice')
def handle_ice(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if room_id:
        emit('webrtc_ice', data, to=room_id, include_self=False)


@socketio.on('leave_vault')
@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    room_id = sid_to_room.pop(sid, None)
    if room_id and room_id in rooms:
        leave_room(room_id)
        rooms[room_id]['users'].pop(sid, None)
        emit('call_ended', {}, to=room_id)
        emit('partner_typing', {'isTyping': False}, to=room_id)
        broadcast_room_status(room_id)

        cleanup_expired_pending(room_id)
        if len(rooms[room_id]['users']) == 0 and len(rooms[room_id]['pending']) == 0:
            rooms.pop(room_id, None)


if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001)

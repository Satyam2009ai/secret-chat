import time
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ghost-vault-super-secret-key'

# async_mode='threading' Python ke saare versions aur Render pe bina error ke chalta hai
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", max_http_buffer_size=5 * 1024 * 1024)

# Pure RAM storage (Server restart ya users leave hote hi sab gayab)
# rooms = { room_id: { 'pin': str, 'users': { sid: name }, 'pending': [ {text, sender, ts} ] } }
rooms = {}
sid_to_room = {}

PENDING_TTL = 600  # Offline RAM message max 10 minute tak rahega, padhte hi delete


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

    # Agar naya room hai toh create karo
    if room_id not in rooms:
        rooms[room_id] = {
            'pin': pin,
            'users': {},
            'pending': []
        }

    room = rooms[room_id]

    # PIN check
    if room['pin'] != pin:
        emit('join_error', {'msg': 'Galat PIN hai! Sahi secret PIN enter karo.'})
        return

    # Strict 2-person room lock
    if len(room['users']) >= 2 and sid not in room['users']:
        emit('join_error', {'msg': 'Room Locked hai! Is room mein already 2 log baat kar rahe hain.'})
        return

    # Join room
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

    # Agar partner ne offline RAM message chhoda tha toh deliver karo aur turant RAM se udao
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
                    'text': msg['text'],
                    'sender': msg['sender'],
                    'senderSid': msg['sender_sid'],
                    'fromRam': True,
                    'autoDestruct': msg.get('autoDestruct', False)
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
    if not text:
        return

    room = rooms[room_id]
    sender_name = room['users'].get(sid, 'Partner')

    if len(room['users']) >= 2:
        # Dono live hain -> Direct relay, ZERO storage
        emit('receive_message', {
            'text': text,
            'sender': sender_name,
            'senderSid': sid,
            'fromRam': False,
            'autoDestruct': auto_destruct
        }, to=room_id)
    else:
        # Partner offline hai -> Temporary RAM buffer (max 10 min, read hote hi wipe)
        cleanup_expired_pending(room_id)
        room['pending'].append({
            'text': text,
            'sender': sender_name,
            'sender_sid': sid,
            'ts': time.time(),
            'autoDestruct': auto_destruct
        })
        emit('receive_message', {
            'text': text,
            'sender': sender_name,
            'senderSid': sid,
            'fromRam': True,
            'autoDestruct': auto_destruct
        })


@socketio.on('send_view_once')
def handle_view_once(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if not room_id or room_id not in rooms:
        return

    image_data = data.get('image')
    if not image_data:
        return

    # View-once photo sirf live partner ko jayegi, RAM mein kabhi store nahi hogi
    if len(rooms[room_id]['users']) < 2:
        emit('system_note', {'msg': 'Partner offline hai! View-Once photo sirf live hone par bhej sakte ho.'})
        return

    sender_name = rooms[room_id]['users'].get(sid, 'Partner')
    emit('receive_view_once', {
        'image': image_data,
        'sender': sender_name,
        'senderSid': sid
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
        # Agar room khaali hai aur koi pending RAM message nahi hai toh poora room memory se delete
        if len(rooms[room_id]['users']) == 0 and len(rooms[room_id]['pending']) == 0:
            rooms.pop(room_id, None)


if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001)
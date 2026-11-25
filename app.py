import time
import uuid
from flask import Flask, render_template, request, Response
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ghost-vault-super-secret-key'

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    max_http_buffer_size=25 * 1024 * 1024
)

# Pure RAM storage (Zero database)
# rooms = {
#   room_id: {
#     'pin': str,
#     'users': { sid: name },
#     'unseen': [ {id, kind, ..., sender, sender_key, senderSid, ts} ],
#     'pending_knock': { 'sender': str, 'sender_key': str, 'ts': float } | None
#   }
# }
rooms = {}
sid_to_room = {}

MAX_UNSEEN_TEXTS = 60
MAX_UNSEEN_FILES = 5
MAX_OFFLINE_FILE_BYTES = 5 * 1024 * 1024  # 5 MB tak ki unseen photo/file RAM mein wait kar sakti hai


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


@app.route('/sw.js')
def service_worker():
    js = """
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      if (clients.length > 0) return clients[0].focus();
      return self.clients.openWindow('/');
    })
  );
});
"""
    return Response(js, mimetype='application/javascript')


@socketio.on('join_vault')
def handle_join(data):
    room_id = str(data.get('room', '')).strip().lower()
    pin = str(data.get('pin', '')).strip()
    name = str(data.get('name', '')).strip()
    sid = request.sid

    if not name:
        emit('join_error', {'msg': 'Apna Nickname daalo (dono partner alag naam rakho, jaise R aur P)!'})
        return

    if not room_id or len(pin) < 4:
        emit('join_error', {'msg': 'Room name aur kam se kam 4-digit PIN daalo!'})
        return

    if room_id not in rooms:
        rooms[room_id] = {
            'pin': pin,
            'users': {},
            'unseen': [],
            'pending_knock': None
        }

    room = rooms[room_id]

    if room['pin'] != pin:
        emit('join_error', {'msg': 'Galat PIN hai! Sahi secret PIN enter karo.'})
        return

    if len(room['users']) >= 2 and sid not in room['users']:
        emit('join_error', {'msg': 'Room Locked hai! Is room mein already 2 log baat kar rahe hain.'})
        return

    # Ensure both partners have distinct nicknames so Seen tracking never confuses sender & receiver
    for existing_sid, existing_name in room['users'].items():
        if existing_sid != sid and existing_name.lower() == name.lower():
            emit('join_error', {'msg': 'Ye nickname tumhare partner ne liya hai! Apna alag nickname daalo.'})
            return

    join_room(room_id)
    room['users'][sid] = name
    sid_to_room[sid] = room_id
    my_key = name.lower()

    emit('join_success', {
        'room': room_id,
        'mySid': sid,
        'myName': name
    })

    broadcast_room_status(room_id)

    # Agar partner ne offline rehne pe Knock bheja tha toh ab deliver karo
    pk = room.get('pending_knock')
    if pk and pk['sender_key'] != my_key:
        emit('knock_received', {
            'sender': pk['sender'],
            'msg': 'Study Reminder: Physics notes update check kar lo!'
        })
        room['pending_knock'] = None

    # Deliver all UNSEEN messages/files jo partner ne bheje the (jab tak seen nahi honge, RAM se delete nahi honge)
    unseen_for_me = [item for item in room['unseen'] if item['sender_key'] != my_key]
    if unseen_for_me:
        for item in unseen_for_me:
            if item['kind'] == 'text':
                emit('receive_message', {
                    'id': item['id'],
                    'text': item['text'],
                    'sender': item['sender'],
                    'senderSid': item['senderSid'],
                    'fromRam': True,
                    'autoHide': item.get('autoHide', False),
                    'oneTime': False,
                    'edited': item.get('edited', False),
                    'seen': False
                })
            elif item['kind'] == 'file':
                emit('receive_file', {
                    'id': item['id'],
                    'fileData': item['fileData'],
                    'fileName': item['fileName'],
                    'fileType': item['fileType'],
                    'fileSize': item['fileSize'],
                    'sender': item['sender'],
                    'senderSid': item['senderSid'],
                    'oneTime': False,
                    'autoHide': item.get('autoHide', False),
                    'seen': False
                })


@socketio.on('send_message')
def handle_message(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if not room_id or room_id not in rooms:
        return

    text = str(data.get('text', '')).strip()
    auto_hide = bool(data.get('autoHide', False))
    one_time = bool(data.get('oneTime', False))
    if not text:
        return

    room = rooms[room_id]
    sender_name = room['users'].get(sid, 'Partner')
    sender_key = sender_name.lower()
    msg_id = uuid.uuid4().hex[:10]

    if one_time and len(room['users']) < 2:
        emit('system_note', {'msg': 'Partner offline hai! 1x Secret Message sirf live hone par bhej sakte ho.'})
        return

    # Non-oneTime messages hamesha RAM unseen queue mein rahenge jab tak partner Seen na kar le
    if not one_time:
        text_items = [x for x in room['unseen'] if x['kind'] == 'text']
        if len(text_items) >= MAX_UNSEEN_TEXTS:
            # Remove oldest text item to keep RAM safe
            for idx, x in enumerate(room['unseen']):
                if x['kind'] == 'text':
                    room['unseen'].pop(idx)
                    break

        room['unseen'].append({
            'id': msg_id,
            'kind': 'text',
            'text': text,
            'sender': sender_name,
            'sender_key': sender_key,
            'senderSid': sid,
            'autoHide': auto_hide,
            'edited': False,
            'ts': time.time()
        })

    emit('receive_message', {
        'id': msg_id,
        'text': text,
        'sender': sender_name,
        'senderSid': sid,
        'fromRam': len(room['users']) < 2,
        'autoHide': auto_hide,
        'oneTime': one_time,
        'edited': False,
        'seen': False
    }, to=room_id)


@socketio.on('mark_seen')
def handle_mark_seen(data):
    sid = request.sid
    room_id = sid_to_room.get(sid)
    if not room_id or room_id not in rooms:
        return

    msg_ids = set(data.get('msgIds', []))
    if not msg_ids:
        return

    room = rooms[room_id]
    viewer_name = room['users'].get(sid, '')
    viewer_key = viewer_name.lower()

    actually_seen = []
    remaining = []
    for item in room['unseen']:
        # Sirf partner (non-sender) hi message ko Seen mark karke RAM se wipe kar sakta hai
        if item['id'] in msg_ids and item['sender_key'] != viewer_key:
            actually_seen.append(item['id'])
        else:
            remaining.append(item)

    room['unseen'] = remaining

    if actually_seen:
        emit('messages_seen', {
            'msgIds': actually_seen,
            'seenBy': viewer_name
        }, to=room_id)


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

    editor_key = rooms[room_id]['users'].get(sid, '').lower()

    # Agar message abhi unseen queue mein hai toh wahan bhi update karo
    for item in rooms[room_id]['unseen']:
        if item['id'] == msg_id and item['kind'] == 'text' and item['sender_key'] == editor_key:
            item['text'] = new_text
            item['edited'] = True

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
    auto_hide = bool(data.get('autoHide', False))

    if not file_data:
        return

    room = rooms[room_id]
    sender_name = room['users'].get(sid, 'Partner')
    sender_key = sender_name.lower()
    partner_online = len(room['users']) >= 2

    if one_time and not partner_online:
        emit('system_note', {'msg': 'Partner offline hai! One-Time photo/file sirf live hone par bhej sakte ho.'})
        return

    if not partner_online and file_size > MAX_OFFLINE_FILE_BYTES:
        emit('system_note', {'msg': 'Partner offline hai! 5 MB se badi file sirf live hone par bhej sakte ho.'})
        return

    file_id = uuid.uuid4().hex[:10]

    # Normal photo/file (<= 5 MB) ko unseen RAM queue mein rakho jab tak partner seen na kar le
    if not one_time and file_size <= MAX_OFFLINE_FILE_BYTES:
        file_items = [x for x in room['unseen'] if x['kind'] == 'file']
        if len(file_items) >= MAX_UNSEEN_FILES:
            for idx, x in enumerate(room['unseen']):
                if x['kind'] == 'file':
                    room['unseen'].pop(idx)
                    break

        room['unseen'].append({
            'id': file_id,
            'kind': 'file',
            'fileData': file_data,
            'fileName': file_name,
            'fileType': file_type,
            'fileSize': file_size,
            'sender': sender_name,
            'sender_key': sender_key,
            'senderSid': sid,
            'autoHide': auto_hide,
            'ts': time.time()
        })

    emit('receive_file', {
        'id': file_id,
        'fileData': file_data,
        'fileName': file_name,
        'fileType': file_type,
        'fileSize': file_size,
        'sender': sender_name,
        'senderSid': sid,
        'oneTime': one_time,
        'autoHide': auto_hide,
        'seen': False
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
    if not room_id or room_id not in rooms:
        return

    room = rooms[room_id]
    sender_name = room['users'].get(sid, 'Partner')

    if len(room['users']) >= 2:
        # Partner connected hai (ya minimized hai) -> Direct alert bhejo
        emit('knock_received', {
            'sender': sender_name,
            'msg': 'Study Reminder: Physics notes update check kar lo!'
        }, to=room_id, include_self=False)
        emit('knock_status', {
            'delivered': True,
            'msg': 'Partner ke phone par disguised notification pahunch gaya!'
        })
    else:
        # Partner offline hai -> Hold in RAM & sender ko honest status batao
        room['pending_knock'] = {
            'sender': sender_name,
            'sender_key': sender_name.lower(),
            'ts': time.time()
        }
        emit('knock_status', {
            'delivered': False,
            'msg': 'Partner abhi offline hai! Jaise hi wo room kholenge, alert + unseen SMS mil jayenge.'
        })


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

        # Agar room khaali hai aur koi unseen message ya knock pending nahi hai toh room delete
        if (
            len(rooms[room_id]['users']) == 0
            and len(rooms[room_id]['unseen']) == 0
            and rooms[room_id]['pending_knock'] is None
        ):
            rooms.pop(room_id, None)


if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001)

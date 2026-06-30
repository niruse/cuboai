import sys
import time
import os

from cuboai_transport_py import PureSession
from cuboai_pure import build_audio_data

uid = os.environ.get('CUBOAI_UID', 'YOUR_UID').strip('"')
account = os.environ.get('CUBOAI_ACCOUNT', 'admin@YOUR_ACCOUNT').strip('"')
password = os.environ.get('CUBOAI_PASSWORD', 'YOUR_PASSWORD').strip('"')
camera_ip = os.environ.get('CUBOAI_CAMERA_IP', '192.168.1.100').strip('"')

sess = PureSession(uid=uid, account=account, password=password, camera_ip=camera_ip)
sess.connect()

inner = sess._inner
chunk_bytes = 576
sent = 0

print("Connected! Sending fake audio data...")
for i in range(50): # send ~2 seconds of empty/noise audio
    chunk = b'\x55' * chunk_bytes
    if inner._sock and inner.session_hdr:
        inner._sock.sendto(
            build_audio_data(inner._R, inner._seq, inner._relseq, sent, chunk),
            inner._cam)
        inner._seq += 1
        inner._relseq += 1
        sent += 1
        inner._drain_acks()
    time.sleep(0.04)
print("Finished sending audio.")
sess.disconnect()

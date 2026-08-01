import json
import struct

def send_pdu(sock, message):
    data = json.dumps(message).encode("utf-8")
    length = struct.pack(">I", len(data))
    sock.sendall(length + data)

def recv_exact(sock, size):
    data = b''

    while len(data) < size:
        packet = sock.recv(size - len(data))

        if not packet:
            return None

        data += packet

    return data


def receive_pdu(sock):
    # Read 4-byte length
    raw_length = recv_exact(sock, 4)

    if raw_length is None:
        return None

    length = struct.unpack(">I", raw_length)[0]

    # Read JSON payload
    raw_data = recv_exact(sock, length)

    if raw_data is None:
        return None

    return json.loads(raw_data.decode("utf-8"))
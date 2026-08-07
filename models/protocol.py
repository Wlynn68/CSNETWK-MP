import json
import struct
import datetime

VERBOSE = False


def set_verbose(enabled: bool):
    global VERBOSE
    VERBOSE = enabled


def is_verbose() -> bool:
    return VERBOSE


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _format_pdu(pdu: dict) -> str:
    pdu_type = pdu.get("type", "UNKNOWN")
    seq = pdu.get("seq_num", "?")
    compact = json.dumps(pdu, separators=(",", ":"))
    if len(compact) > 200:
        pretty = json.dumps(pdu, indent=2)
        return f"[{pdu_type}] seq={seq}\n{pretty}"
    return f"[{pdu_type}] seq={seq} {compact}"


def log_send(label: str, pdu: dict):
    if not VERBOSE:
        return
    ts = _timestamp()
    print(f"\n  ╔═ SEND {label} @ {ts}")
    print(f"  ║ {_format_pdu(pdu)}")
    print(f"  ╚{'═' * 60}")


def log_recv(label: str, pdu: dict):
    if not VERBOSE:
        return
    ts = _timestamp()
    print(f"\n  ╔═ RECV {label} @ {ts}")
    print(f"  ║ {_format_pdu(pdu)}")
    print(f"  ╚{'═' * 60}")


def send_pdu(sock, message):
    data = json.dumps(message).encode("utf-8")
    length = struct.pack(">I", len(data))
    sock.sendall(length + data)
    if VERBOSE:
        peer = ""
        try:
            peer = str(sock.getpeername())
        except Exception:
            pass
        log_send(peer, message)


def recv_exact(sock, size):
    data = b''

    while len(data) < size:
        packet = sock.recv(size - len(data))

        if not packet:
            return None

        data += packet

    return data


def receive_pdu(sock):
    raw_length = recv_exact(sock, 4)

    if raw_length is None:
        return None

    length = struct.unpack(">I", raw_length)[0]

    raw_data = recv_exact(sock, length)

    if raw_data is None:
        return None

    pdu = json.loads(raw_data.decode("utf-8"))
    if VERBOSE:
        peer = ""
        try:
            peer = str(sock.getpeername())
        except Exception:
            pass
        log_recv(peer, pdu)
    return pdu
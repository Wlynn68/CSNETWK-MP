import json
import struct
import datetime

VERBOSE = False

# RFC 0001 §5.2: "A PDU MUST NOT exceed 65,535 bytes."
MAX_PDU_SIZE = 65_535


class InvalidPDUError(Exception):
    """Raised when received bytes cannot be parsed as valid UTF-8 JSON.

    This corresponds to RFC 0001 §11 ERROR code INVALID_JSON. The frame
    length prefix was read successfully and exactly that many bytes were
    consumed, so the connection's framing stays in sync — the caller MAY
    report the error and keep the connection open (RFC 0001 §11: "The
    server MUST NOT disconnect a client solely because it received an
    illegal action PDU.").
    """


class PDUTooLargeError(InvalidPDUError):
    """Raised when a frame's declared length exceeds MAX_PDU_SIZE.

    Unlike InvalidPDUError, the oversized body is deliberately NOT read
    off the socket (to avoid an unbounded/blocking read), so framing
    cannot be trusted to stay in sync afterward. Callers should treat
    this as fatal and close the connection.
    """


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
    """Read and parse one PDU. Returns the parsed dict, or None if the peer
    closed the connection cleanly (EOF on the length prefix or body).

    Raises:
        PDUTooLargeError: the declared frame length exceeds MAX_PDU_SIZE.
            The body is not read; the connection can no longer be trusted
            to be in sync and should be closed.
        InvalidPDUError: the frame was read in full but its bytes are not
            valid UTF-8 JSON. Framing stays in sync; safe to keep reading.
    """
    raw_length = recv_exact(sock, 4)

    if raw_length is None:
        return None

    length = struct.unpack(">I", raw_length)[0]

    if length > MAX_PDU_SIZE:
        raise PDUTooLargeError(
            f"Declared PDU length {length} exceeds the {MAX_PDU_SIZE}-byte limit (RFC 0001 §5.2)."
        )

    raw_data = recv_exact(sock, length)

    if raw_data is None:
        return None

    try:
        pdu = json.loads(raw_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InvalidPDUError(f"Malformed PDU: {e}") from e

    if not isinstance(pdu, dict):
        raise InvalidPDUError("Malformed PDU: top-level JSON value must be an object.")

    if VERBOSE:
        peer = ""
        try:
            peer = str(sock.getpeername())
        except Exception:
            pass
        log_recv(peer, pdu)
    return pdu

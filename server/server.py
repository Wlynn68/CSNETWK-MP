import socket
import threading

from models.protocol import receive_pdu, send_pdu

HOST = "127.0.0.1"
PORT = 4444

clients = []


def handle_client(conn, addr):

    print(f"[CONNECTED] {addr}")

    while True:

        message = receive_pdu(conn)

        if message is None:
            break

        print("Received:", message)

        # test
        if message["type"] == "PING":

            response = {
                "type": "PONG",
                "seq_num": message.get("seq_num", 0)
            }

            send_pdu(conn, response)

    print(f"[DISCONNECTED] {addr}")

    conn.close()


def main():

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    server.bind((HOST, PORT))

    server.listen(2)

    print(f"Listening on {HOST}:{PORT}")

    while True:

        conn, addr = server.accept()

        if len(clients) >= 2:

            send_pdu(conn, {
                "type": "ERROR",
                "message": "Server full"
            })

            conn.close()
            continue

        clients.append(conn)

        thread = threading.Thread(
            target=handle_client,
            args=(conn, addr),
            daemon=True
        )

        thread.start()


if __name__ == "__main__":
    main()
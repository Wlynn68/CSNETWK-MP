import socket

from models.protocol import send_pdu, receive_pdu

HOST = "127.0.0.1"
PORT = 4444


def main():

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    client.connect((HOST, PORT))

    print("Connected!")

    send_pdu(client, {
        "type": "PING",
        "seq_num": 1
    })

    response = receive_pdu(client)

    print("Server replied:")

    print(response)

    client.close()


if __name__ == "__main__":
    main()
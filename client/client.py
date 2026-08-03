import socket
import threading

from models.protocol import send_pdu, receive_pdu
from models.pdu import *

HOST = "127.0.0.1"
PORT = 4444
seq_num = 1

def receiver(sock):

    while True:
        msg = receive_pdu(sock)

        if msg is None:
            break

        print("\nReceived")
        print(msg)

def main():

    global seq_num
    
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect((HOST, PORT))

    print("Connected!")

    threading.Thread(
        target=receiver,
        args=(client,),
        daemon=True
    ).start()

    player = input("Please input your Player ID: ")

    send_pdu(client, player_ready(player, "temp deck", seq_num))

    seq_num += 1
    response = receive_pdu(client)

    print("Server replied:")
    print(response)

    client.close()


if __name__ == "__main__":
    main()
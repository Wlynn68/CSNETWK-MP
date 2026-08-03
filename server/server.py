import socket
import threading

from models.protocol import receive_pdu, send_pdu
from models.pdu import *

HOST = "127.0.0.1"
PORT = 4444

clients = []
players = {}

#send message to all clients
def broadcast(message):
    for client in clients:
        try:
            send_pdu(client, message)
        except:
            pass

def handle_client(conn, addr):

    print(f"[CONNECTED] {addr}")

    while True:
        try:
            message = receive_pdu(conn)

            if message is None:
                break

            print("Received:", message)
            msg_type = message["type"]
            player_id = message["state"].split(None, 1)[0] # get player's id

            if msg_type == "GAME_STATE_UPDATE":
                if player_id in players:
                    send_pdu(conn, error("DUPLICATE PLAYER", "Player ID already taken", "", message["seq_num"]))

            if msg_type == "PING":
                send_pdu(conn, pong( message["timestamp"], message["seq_num"]))
            
            elif msg_type == "PLAYER_READY":    
                players[player_id] = conn
                print(f"{player_id} is ready.")

            broadcast(game_state_update({},message["seq_num"] + 1))

            if len(players) == 2:
                broadcast(game_state_update("We have 2 players.",message["seq_num"] + 1))

        except Exception as e:
            print(e)
            break

    print(f"[DISCONNECTED] {addr}")

    conn.close()

    if conn in clients:
        clients.remove(conn)

    for pid, sock in list(players.items()):
        if sock == conn:
            del players[pid]

def main():

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((HOST, PORT))
    server.listen(2)

    print(f"Listening on {HOST}:{PORT}")

    while True:

        conn, addr = server.accept()

        if len(clients) >= 2:

            send_pdu(conn, error("LOBBY FULL", "Lobby is full. Please try again later.", "", 0))
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
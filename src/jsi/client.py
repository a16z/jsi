# client.py

import os
import socket
import sys

from jsi.server import SOCKET_PATH


def send_command(command: str):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            client.connect(SOCKET_PATH.as_posix())
            client.sendall(command.encode())
            response = client.recv(4096).decode()
            print(response)
        except OSError as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python client.py <command>")
        sys.exit(1)

    command = " ".join(sys.argv[1:])
    send_command(os.path.abspath(command))

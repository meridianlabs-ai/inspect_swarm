"""What the bridged users-isolation tests run in the sandbox, as root or as a member.

Usage: python3 /opt/checks.py <check> <args>

- `binds <port>`: bind at the port as asyncio does (SO_REUSEADDR), with and without SO_REUSEPORT, at ::1, 127.0.0.1, :: and 0.0.0.0, printing each address, the option and the result.
- `listener <port>`: the pid of the process listening at the port, from the socket tables and each process's descriptors.
- `connected <host> <local port> <port> <user>`: wait up to 30 seconds for the user's established connection from the local port to the port, printing `connected` or `missing`.
- `retire <host> <local port> <port>`: connect from a fixed local port, send a model request with the marker native-retired, and reset the connection.
- `listen <host> <port> <log>`: listen at the port, writing `listening` and then `accepted` for each connection to the log.
"""

import errno
import os
import pwd
import socket
import struct
import sys
import time


def binds(port: int) -> None:
    for family, host in (
        (socket.AF_INET6, "::1"),
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET6, "::"),
        (socket.AF_INET, "0.0.0.0"),
    ):
        for reuseport in (0, 1):
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, reuseport)
            try:
                s.bind((host, port))
                s.listen()
                print(host, reuseport, "bound")
            except OSError as ex:
                print(host, reuseport, errno.errorcode.get(ex.errno or 0, str(ex)))
            s.close()


def listener(port: int) -> None:
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        for row in open(table).read().splitlines()[1:]:
            fields = row.split()
            if fields[1].endswith(f":{port:04X}") and fields[3] == "0A":
                inodes.add(f"socket:[{fields[9]}]")
    pids: set[str] = set()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
            if any(os.readlink(f"/proc/{pid}/fd/{fd}") in inodes for fd in fds):
                pids.add(pid)
        except OSError:
            pass
    print(*sorted(pids))


def connected(host: str, local_port: int, port: int, user: str) -> None:
    table = "/proc/net/tcp6" if ":" in host else "/proc/net/tcp"
    uid = str(pwd.getpwnam(user).pw_uid)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for row in open(table).read().splitlines()[1:]:
            fields = row.split()
            if (
                fields[1].endswith(f":{local_port:04X}")
                and fields[2].endswith(f":{port:04X}")
                and fields[3] == "01"
                and fields[7] == uid
            ):
                print("connected")
                return
        time.sleep(0.1)
    print("missing")


def retire(host: str, local_port: int, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    s.bind((host, local_port))
    s.connect((host, port))
    body = b'{"model": "inspect", "max_tokens": 16, "messages": [{"role": "user", "content": "native-retired"}]}'
    head = f"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\ncontent-type: application/json\r\ncontent-length: {len(body)}\r\n\r\n"
    s.sendall(head.encode() + body)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    s.close()
    print("reset")


def listen(host: str, port: int, log: str) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    s.bind((host, port))
    s.listen()
    with open(log, "a") as out:
        print("listening", file=out, flush=True)
        while True:
            c, _ = s.accept()
            print("accepted", file=out, flush=True)
            c.close()


if __name__ == "__main__":
    check, *args = sys.argv[1:]
    if check == "binds":
        binds(int(args[0]))
    elif check == "listener":
        listener(int(args[0]))
    elif check == "connected":
        connected(args[0], int(args[1]), int(args[2]), args[3])
    elif check == "retire":
        retire(args[0], int(args[1]), int(args[2]))
    elif check == "listen":
        listen(args[0], int(args[1]), args[2])
    else:
        sys.exit(f"unknown check {check}")

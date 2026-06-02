from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO
import random
import threading
import serial
import sys

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"

socketio = SocketIO(app, async_mode="threading")

NUM_CELLS = 48

# Initial state
cell_states = states = [random.choice([0, 1]) for _ in range(48)]


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/control")
def control():
    return render_template("control.html")


@app.route("/randomize")
def randomize():
    global cell_states

    # Generate 48 new states
    cell_states = [
        random.choice([0, 1])
        for _ in range(NUM_CELLS)
    ]

    # Broadcast all 48 values in one websocket message
    for x in [1, 2]:
        socketio.emit(
            "table_update",
            {
                "switch_id": x,
                "states": cell_states
            }
        )

    return jsonify(cell_states)


timer_running = False
timer_seconds = 0
player1_done = False
player2_done = False
player1_time = 0
player2_time = 0
timer_thread = None
stop_serial = False


def timer_loop():
    global timer_seconds, timer_running
    global player1_time, player1_done
    global player2_time, player2_done

    while timer_running:
        socketio.emit(
            "clock_update",
            {
                "seconds1": player1_time,
                "seconds2": player2_time
            }
        )

        socketio.sleep(1)
        timer_seconds += 1
        if not player1_done:
            player1_time = timer_seconds
        if not player2_done:
            player2_time = timer_seconds

        if player1_done and player2_done:
            timer_running = False

def serial_loop(serial_id: int = 0) -> None:
    print(f"Restarted {serial_id}")
    ser = serial.Serial(
        port=f'/dev/ttyACM{serial_id}',
        baudrate=9600,
        parity=serial.PARITY_ODD,
        stopbits=serial.STOPBITS_TWO,
        bytesize=serial.SEVENBITS,
        timeout=5
    )
    while not stop_serial:
        line = ser.readline()
        data = line.decode("ascii").strip("\r\n ").split(" ")
        data.reverse()
        cell_states = [x != "FF" for x in data]

        socketio.emit(
            "table_update",
            {
                "switch_id": serial_id + 1,
                "states": cell_states
            }
        )
        for x in cell_states:
            if not x:
                break
        else:
            if serial_id == 0:
                global player1_done
                player1_done = True
            else:
                global player2_done
                player2_done = True




@app.route("/start-clock")
def start_clock():
    global timer_running, timer_thread

    if not timer_running:
        timer_running = True

        timer_thread = threading.Thread(
            target=timer_loop,
            daemon=True
        )
        timer_thread.start()

    return "Clock started"


@app.route("/stop-clock1")
def stop_clock1():
    global player1_done

    player1_done = True

    return "Clock stopped"

@app.route("/stop-clock2")
def stop_clock2():
    global player2_done

    player2_done = True

    return "Clock stopped"


@app.route("/reset-clock")
def reset_clock():
    global timer_seconds, timer_running
    global player1_time, player1_done
    global player2_time, player2_done

    timer_seconds = 0
    player1_time = 0
    player1_done = False
    player2_time = 0
    player2_done = False
    timer_running = False

    socketio.emit(
        "clock_update",
        {
            "seconds1": player1_time,
            "seconds2": player2_time
        }
    )

    return "Clock reset"

@app.route("/restart-serial")
def restart_serial():
    global stop_serial, serial_threads
    stop_serial = True
    for t in serial_threads:
        t.join()
    print("Threads dead")
    serial_threads = None
    reset_serial_thread()
    return "Done"



serial_threads = None

def reset_serial_thread():
    global serial_threads, stop_serial

    stop_serial = False

    if serial_threads is not None:
        raise Exception("Stuff")

    serial_threads = [ threading.Thread(
        target=serial_loop,
        args=(x,),
        daemon=True
    ) for x in [0,1]]
    for t in serial_threads:
        t.start()


@socketio.on("connect")
def handle_connect():
    socketio.emit(
        "clock_update",
        {
            "seconds1": player1_time,
            "seconds2": player2_time
        }
    )
    # Send current state immediately
    for x in [1, 2]:
        socketio.emit(
            "table_update",
            {
                "switch_id": x,
                "states": cell_states
            }
        )
    if serial_threads is None:
        reset_serial_thread()


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", debug=True)

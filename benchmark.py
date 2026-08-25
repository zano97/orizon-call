import time
import queue
import threading
import numpy as np
from audio_recorder import AudioRecorder

# A mock for measuring the write loop performance
def mock_write_loop():
    # Setup
    recorder = AudioRecorder()
    recorder._open_output_file = lambda x: None
    recorder._output_file = None

    stop_event = threading.Event()
    mic_q = queue.Queue()
    sys_q = queue.Queue()

    # Pre-fill queues with small chunks to simulate incoming audio stream
    num_chunks = 5000
    chunk_size = 480 # 10ms at 48kHz
    for _ in range(num_chunks):
        mic_q.put((np.zeros((chunk_size, 1), dtype=np.float32), 48000.0))
        sys_q.put((np.zeros((chunk_size, 2), dtype=np.float32), 48000.0))

    # Add poison pill equivalent to exit test early without actually triggering stop_event immediately
    # We'll run the loop in a thread, then set stop_event after a small delay
    def stopper():
        time.sleep(1.0)
        stop_event.set()

    t = threading.Thread(target=stopper)
    t.start()

    start = time.time()
    try:
        recorder._writer_loop(
            stop_event=stop_event,
            mic_q=mic_q,
            sys_q=sys_q,
            mic_prelude=[],
            sys_prelude=[],
            session_has_sys=True
        )
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

    end = time.time()
    t.join()

    print(f"Write loop duration: {end - start:.4f}s")

if __name__ == '__main__':
    mock_write_loop()

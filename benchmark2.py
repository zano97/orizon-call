import time
import queue
import threading
import numpy as np
from audio_recorder import AudioRecorder

# A mock for measuring the write loop CPU utilization
def benchmark_cpu():
    recorder = AudioRecorder()
    recorder._output_file = None

    stop_event = threading.Event()
    mic_q = queue.Queue()
    sys_q = queue.Queue()

    # We want to measure the performance when queues are constantly empty
    # to see the impact of time.sleep(0.01) polling vs condition variables
    start = time.perf_counter()
    start_cputime = time.process_time()

    def stopper():
        time.sleep(2.0)
        stop_event.set()

    t = threading.Thread(target=stopper)
    t.start()

    # Create a background thread that puts data in very slowly to simulate real audio
    def producer():
        while not stop_event.is_set():
            time.sleep(0.02) # 20ms
            mic_q.put((np.zeros((960, 1), dtype=np.float32), 48000.0))
            sys_q.put((np.zeros((960, 2), dtype=np.float32), 48000.0))

    p = threading.Thread(target=producer)
    p.start()

    recorder._writer_loop(
        stop_event=stop_event,
        mic_q=mic_q,
        sys_q=sys_q,
        mic_prelude=[],
        sys_prelude=[],
        session_has_sys=True
    )

    end = time.perf_counter()
    end_cputime = time.process_time()

    t.join()
    p.join()

    print(f"Total time: {end - start:.4f}s")
    print(f"CPU time: {end_cputime - start_cputime:.4f}s")
    print(f"CPU usage: {(end_cputime - start_cputime) / (end - start) * 100:.2f}%")

if __name__ == '__main__':
    benchmark_cpu()

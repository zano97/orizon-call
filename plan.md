1. **Analyze:**
   - The current code uses a hardcoded `time.sleep(0.01)` in `_writer_loop` when no data is written.
   - This polling loop burns unnecessary CPU cycles, especially since threads could be awakened precisely when new data is available.
   - We can replace polling with a `threading.Condition` or a simpler wait mechanism (like modifying `_pause_event.wait()` or waiting on the `queue.Queue` itself or an event signaled on `put`).

2. **Measure:**
   - I already ran a benchmark in `benchmark2.py` which showed 3.89% CPU usage in the polling loop when data is being processed slowly (20ms intervals).

3. **Plan:**
   - Add a `_data_event` (`threading.Event`) to `AudioRecorder` initialization.
   - Set `_data_event.set()` in `_route_chunk` when adding data to `_mic_queue` or `_sys_queue`.
   - Update `_writer_loop` to use `_data_event.wait(timeout=0.1)` (and then clear it with `_data_event.clear()`) instead of `time.sleep(0.01)`.
   - Since `_route_chunk` is called from audio callbacks, it's safe to call `set()`. Wait should happen only if we couldn't write anything.
   - Better yet, instead of `_data_event`, we can block on `q.get(timeout=0.01)` in `_ingest`, but `_writer_loop` checks both queues and has a complex starvation logic.
   - A `_data_event` is safer and fits perfectly without changing the starvation logic.

4. **Implementation:**
   - `__init__`: `self._data_event = threading.Event()`
   - `start()`: re-initialize `self._data_event = threading.Event()` just like `_stop_event` if needed (or just clear it). Actually, it's better to just reuse it and `clear()` it on start, or recreate it.
   - `_route_chunk`:
     ```python
     try:
         q.put_nowait((chunk, rate))
         self._data_event.set()
     ```
   - `_writer_loop`:
     Change:
     ```python
                 if not wrote:
                     time.sleep(0.01)
     ```
     To:
     ```python
                 if not wrote:
                     self._data_event.wait(timeout=0.05)
                     self._data_event.clear()
     ```
     Wait: Wait shouldn't be too long because of disk space check and flush. 0.05s is fine (50ms). Actually, we can just replace `time.sleep(0.01)` with `self._data_event.wait(timeout=0.01)` and clear it beforehand.
     Let's refine:
     ```python
                 if not wrote:
                     self._data_event.clear()
                     self._data_event.wait(timeout=0.05)
     ```
     Wait, if we clear it after checking `wrote`, we might miss an event set between `wrote = False` and `clear()`.
     We can do:
     ```python
                 # At the start of loop
                 self._data_event.clear()
                 ...
                 if not wrote:
                     self._data_event.wait(timeout=0.05)
     ```

5. **Verify:**
   - Run `pytest` again.
   - Run `benchmark2.py` again to see CPU usage drop.

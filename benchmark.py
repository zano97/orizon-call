import timeit

setup = """
devices = [{'name': f'device_{i}', 'max_input_channels': 2, 'default_samplerate': 48000} for i in range(100)]
pulse_names = ['NOT_FOUND_1', 'NOT_FOUND_2', 'NOT_FOUND_3', 'DEVICE_99']
"""

old_code = """
for candidate in pulse_names:
    cand_lower = candidate.lower()
    for i, dev in enumerate(devices):
        dev_lower = dev['name'].lower()
        if dev['max_input_channels'] > 0 and (
                cand_lower in dev_lower or dev_lower in cand_lower):
            break
"""

new_code = """
devices_lower = [(i, dev, dev['name'].lower()) for i, dev in enumerate(devices)]
for candidate in pulse_names:
    cand_lower = candidate.lower()
    for i, dev, dev_lower in devices_lower:
        if dev['max_input_channels'] > 0 and (
                cand_lower in dev_lower or dev_lower in cand_lower):
            break
"""

n = 10000
t_old = timeit.timeit(old_code, setup=setup, number=n)
t_new = timeit.timeit(new_code, setup=setup, number=n)

print(f"Old code: {t_old:.4f} seconds")
print(f"New code: {t_new:.4f} seconds")
print(f"Improvement: {((t_old - t_new) / t_old) * 100:.2f}%")

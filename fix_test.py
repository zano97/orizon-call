import re

with open('tests/test_output_files.py', 'r') as f:
    content = f.read()

content = content.replace('monkeypatch.setattr(shutil, "disk_usage", lambda path: mock_usage)',
                          'monkeypatch.setattr("audio_recorder.shutil.disk_usage", lambda path: mock_usage)')

with open('tests/test_output_files.py', 'w') as f:
    f.write(content)

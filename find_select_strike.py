import os

files_with_import = []
for root, dirs, files in os.walk('.'):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            try:
                content = open(path, errors='ignore').read()
                if 'select_strike' in content:
                    files_with_import.append(path)
            except Exception:
                pass

print("Files importing/using select_strike:")
for f in files_with_import:
    print("-", f)

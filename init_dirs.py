import os

directories = [
    "chvk_city/backend/api",
    "chvk_city/backend/services",
    "chvk_city/backend/models",
    "chvk_city/backend/database",
    "chvk_city/bot/telegram",
    "chvk_city/utils",
]

for directory in directories:
    os.makedirs(directory, exist_ok=True)
    # Create __init__.py files
    open(os.path.join(directory, "__init__.py"), "a").close()

# Create __init__.py in parent directories if they don't exist
open("chvk_city/__init__.py", "a").close()
open("chvk_city/backend/__init__.py", "a").close()
open("chvk_city/bot/__init__.py", "a").close()

print("Directories created successfully.")

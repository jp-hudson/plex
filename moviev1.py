#!/Users/jhudson/miniconda3/bin/python

import os
import sys
import re

def clean_filename(filename):
    if not filename.lower().endswith(".mkv"):
        return None

    base, ext = os.path.splitext(filename)

    # Cut off everything after 1080p, 720p, 2160p, etc.
    base = re.sub(r'[\s\(]*\b(1080p|720p|480p|2160p|WEB|BluRay|HDRip|HDTV)\b.*', '', base)

    # Replace dots with spaces
    base = base.replace('.', ' ')

    # Clean up extra whitespace
    base = re.sub(r'\s+', ' ', base).strip()

    return base + ext

def process_folder(folder):
    if not os.path.isdir(folder):
        print(f"Error: {folder} is not a valid directory.")
        return

    for filename in os.listdir(folder):
        old_path = os.path.join(folder, filename)
        if os.path.isfile(old_path):
            new_name = clean_filename(filename)
            if new_name and new_name != filename:
                new_path = os.path.join(folder, new_name)
                print(f"Renaming:\n  {filename}\n  --> {new_name}")
                os.rename(old_path, new_path)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python movie_sorter.py <foldername>")
        sys.exit(1)

    foldername = sys.argv[1]
    process_folder(foldername)


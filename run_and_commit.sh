#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Configuration ---
YOUTUBE_URL="https://www.youtube.com/@sunriches/videos"
OUTPUT_DIR="2.sunrich"
COMMIT_MESSAGE="Automated sync: Update transcripts from @sunriches"

# --- Automation Logic ---

echo "--- Step 1 of 4: Running the video transcript script ---"
python3 get_transcripts.py "$YOUTUBE_URL" --output_dir "$OUTPUT_DIR"

echo "--- Step 2 of 4: Staging changes for Git ---"
# Add the main script, this automation script, and the entire output directory
git add get_transcripts.py run_and_commit.sh "$OUTPUT_DIR"

# Check if there are any changes to commit
if git diff-index --quiet HEAD --; then
  echo "--- No new transcripts or changes found. Nothing to commit. ---"
  exit 0
fi

echo "--- Step 3 of 4: Committing changes ---"
# Commit the changes with a timestamp
git commit -m "$COMMIT_MESSAGE"

echo "--- Step 4 of 4: Pushing to GitHub ---"
# Push to the remote repository
git push

echo "--- All done. Automation script finished successfully. ---" 
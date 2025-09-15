#!/bin/bash

# --- Configuration ---
SCRIPT_NAME="server.py"
EXECUTABLE_NAME="file_server" # Name of the output executable (without extension)
USE_ONEFILE=true             # Set to false to build a folder (directory) instead of a single file
ADD_ICON=false               # Set to true and define ICON_PATH to add an icon
# ICON_PATH="path/to/your/icon.ico" # Example icon path (uncomment and set if ADD_ICON=true)

# Hidden imports for PyInstaller (if auto-detection fails)
# Add modules here that PyInstaller might not find automatically
HIDDEN_IMPORTS=(
    "psutil"
    "markdown"
    # Add more if needed, e.g., "your_custom_module"
)

# Data files/folders to include (format: "source:dest" for Linux/Mac, "source;dest" for Windows)
# This is crucial for including your 'static' folder
DATA_FILES=(
    "static:static" # Includes the 'static' folder from CWD into the dist root
    # Add more data files if needed, e.g., "config.ini:config.ini"
)

# --- Functions ---
print_header() {
    echo "================================"
    echo "  Building Executable: $EXECUTABLE_NAME"
    echo "================================"
}

check_command() {
    if ! command -v "$1" &> /dev/null; then
        echo "Error: $1 is not installed or not found in PATH." >&2
        exit 1
    fi
}

# --- Main Script ---
print_header

echo "Checking for required tools..."
check_command python3
check_command pip
check_command pyinstaller

echo "Upgrading pip and installing required Python packages..."
# Use python3 -m pip to ensure we're using the correct pip
python3 -m pip install --upgrade pip
if ! python3 -m pip install --upgrade pyinstaller psutil markdown; then
    echo "Error: Failed to install or upgrade required Python packages."
    exit 1
fi

echo "Preparing PyInstaller command..."
PYINSTALLER_ARGS=()

# Basic output name
PYINSTALLER_ARGS+=("--name" "$EXECUTABLE_NAME")

# Mode: Onefile or Onedir
if [ "$USE_ONEFILE" = true ]; then
    PYINSTALLER_ARGS+=("--onefile")
    echo "Build mode: Single file (--onefile)"
else
    PYINSTALLER_ARGS+=("--onedir")
    echo "Build mode: Directory (--onedir)"
fi

# Add Icon (if enabled and path is set)
if [ "$ADD_ICON" = true ] && [ -n "$ICON_PATH" ]; then
    if [ -f "$ICON_PATH" ]; then
        PYINSTALLER_ARGS+=("--icon" "$ICON_PATH")
        echo "Adding icon: $ICON_PATH"
    else
        echo "Warning: Icon file '$ICON_PATH' not found. Skipping icon." >&2
    fi
else
    if [ "$ADD_ICON" = true ]; then
        echo "Warning: ADD_ICON is true but ICON_PATH is not set. Skipping icon." >&2
    fi
fi

# Add Hidden Imports
for module in "${HIDDEN_IMPORTS[@]}"; do
    PYINSTALLER_ARGS+=("--hidden-import" "$module")
    echo "Adding hidden import: $module"
done

# Add Data Files/Folders
for data in "${DATA_FILES[@]}"; do
    PYINSTALLER_ARGS+=("--add-data" "$data")
    # Improve readability of data file log
    src_dest=(${data//:/ }) # Split by : (Linux/Mac style) - Windows uses ;
    if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
        src_dest=(${data//;/ }) # Split by ; for Windows
    fi
    echo "Adding data: ${src_dest[0]} -> ${src_dest[1]}"
done

# Add the main script name
PYINSTALLER_ARGS+=("$SCRIPT_NAME")

# Print the full command for debugging/transparency
echo
echo "Running PyInstaller with arguments:"
echo "pyinstaller ${PYINSTALLER_ARGS[*]}"
echo

# Execute PyInstaller
if pyinstaller "${PYINSTALLER_ARGS[@]}"; then
    echo
    echo "Build successful!"
    
    # Determine the output path
    if [ "$USE_ONEFILE" = true ]; then
        OUTPUT_PATH="dist/$EXECUTABLE_NAME"
        # Add platform-specific extension if needed for display
        if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
            OUTPUT_PATH="${OUTPUT_PATH}.exe"
        fi
    else
        OUTPUT_PATH="dist/$EXECUTABLE_NAME/" # Onedir creates a folder
    fi
    
    echo "Executable created at: $OUTPUT_PATH"
    
    # --- Optional: Post-Build Cleanup ---
    # Uncomment the lines below if you want to automatically clean up build artifacts
    # echo "Cleaning up build directories..."
    # rm -rf build/
    # rm -f "$EXECUTABLE_NAME.spec"
    # echo "Cleanup complete."
    
else
    echo
    echo "Error: PyInstaller build failed. Check the output above for details."
    exit 1
fi

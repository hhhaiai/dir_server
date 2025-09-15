#!/bin/bash

# --- macOS PyInstaller Build Script with Auto-Cleanup ---
# Optimized for simplicity and macOS conventions.

set -e # Exit immediately if a command exits with a non-zero status.

# --- Configuration (Easy to change) ---
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" # Get script's directory
SCRIPT_NAME="server.py"
EXECUTABLE_NAME="file_server" # Default name for macOS apps
STATIC_DIR="static"
# --- AUTO_CLEANUP is now enabled by default ---
AUTO_CLEANUP=true # Set to false if you want to keep build/ and .spec files

# --- Logging with Colors (macOS Terminal friendly) ---
log() { echo -e "\033[1m$1\033[0m"; }        # Bold
info() { echo -e "\033[34m[INFO]\033[0m $1"; }  # Blue
success() { echo -e "\033[32m[SUCCESS]\033[0m $1"; } # Green
warn() { echo -e "\033[33m[WARN]\033[0m $1"; }   # Yellow
error() { echo -e "\033[31m[ERROR]\033[0m $1"; }  # Red

# --- Cleanup Function ---
cleanup() {
    if [[ "$AUTO_CLEANUP" == true ]]; then
        info "Cleaning up PyInstaller build artifacts..."
        local build_dir="$PROJECT_DIR/build"
        local spec_file="$PROJECT_DIR/${EXECUTABLE_NAME}.spec"
        
        if [[ -d "$build_dir" ]]; then
            rm -rf "$build_dir"
            info "Removed build directory: $build_dir"
        fi
        
        if [[ -f "$spec_file" ]]; then
            rm -f "$spec_file"
            info "Removed spec file: $spec_file"
        fi
        success "Cleanup completed."
    else
        info "AUTO_CLEANUP is disabled. Keeping build artifacts."
    fi
}

# --- Main Script ---
main() {
    # 1. Verify OS
    if [[ "$(uname)" != "Darwin" ]]; then
        error "This script is intended for macOS only."
        exit 1
    fi
    log "🚀 Starting build process for '$EXECUTABLE_NAME' on macOS..."

    # 2. Check for required tools
    info "Checking for required tools..."
    for cmd in python3 pip pyinstaller; do
        if ! command -v "$cmd" &> /dev/null; then
            error "'$cmd' is not installed or not found in PATH."
            exit 1
        fi
    done
    success "All required tools found."

    # 3. Install/upgrade Python dependencies
    info "Installing/upgrading Python dependencies..."
    if ! python3 -m pip install --upgrade pip pyinstaller psutil markdown; then
        error "Failed to install/upgrade Python packages."
        exit 1
    fi
    success "Python dependencies are up-to-date."

    # 4. Verify project structure
    if [[ ! -f "$PROJECT_DIR/$SCRIPT_NAME" ]]; then
        error "Script '$SCRIPT_NAME' not found in project directory: $PROJECT_DIR"
        exit 1
    fi
    if [[ ! -d "$PROJECT_DIR/$STATIC_DIR" ]]; then
        warn "Static directory '$STATIC_DIR' not found. Building without it. (Code highlighting/Markdown CSS might not work)"
        DATA_ARG=""
    else
        DATA_ARG="--add-data $STATIC_DIR:$STATIC_DIR"
        info "Including static assets: $STATIC_DIR"
    fi

    # 5. Build with PyInstaller
    info "Building executable with PyInstaller..."
    PYINSTALLER_CMD=(
        pyinstaller
        "--onefile" # macOS users often prefer a single file
        "--name" "$EXECUTABLE_NAME"
        "--hidden-import" "psutil"   # Explicitly include
        "--hidden-import" "markdown" # Explicitly include
        $DATA_ARG # Include static dir if it exists
        "$SCRIPT_NAME"
    )
    
    info "Executing: ${PYINSTALLER_CMD[*]}"
    
    # Use a subshell for building so we can trap EXIT specifically for cleanup
    (
        # Set a trap to run cleanup when this subshell exits, regardless of success or failure
        # This ensures cleanup runs even if the build fails partway through
        trap cleanup EXIT 
        
        if "${PYINSTALLER_CMD[@]}"; then
            success "Build completed successfully!"
            
            OUTPUT_PATH="$PROJECT_DIR/dist/$EXECUTABLE_NAME"
            if [[ -f "$OUTPUT_PATH" ]]; then
                success "Executable is located at: $OUTPUT_PATH"
                log "✅ Build process finished for '$EXECUTABLE_NAME'."
                # The 'cleanup' function will be called by the trap when this subshell exits
            else
                error "Build reported success, but executable not found at $OUTPUT_PATH"
                exit 1
            fi
        else
            error "PyInstaller build failed."
            exit 1
        fi
    )
    # The subshell exits here, triggering the 'trap cleanup EXIT'
}

# Run the main function
main "$@"

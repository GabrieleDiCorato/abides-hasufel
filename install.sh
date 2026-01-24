#!/bin/bash
# ABIDES Installation Script
# This project now uses UV for dependency management

# Check if UV is installed
if ! command -v uv &> /dev/null
then
    echo "UV is not installed. Installing UV..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "Please restart your terminal and run this script again."
    exit 1
fi

# Install the project and all dependencies (excluding dev dependencies)
echo "Installing ABIDES with UV..."
uv sync --no-dev

echo "Installation complete!"
echo "To activate the virtual environment, run: source .venv/bin/activate"

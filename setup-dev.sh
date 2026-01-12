#!/bin/bash
# ABIDES Development Environment Setup Script
# This project now uses UV for dependency management

# Check if UV is installed
if ! command -v uv &> /dev/null
then
    echo "UV is not installed. Installing UV..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "Please restart your terminal and run this script again."
    exit 1
fi

# Install the project with development dependencies
echo "Setting up ABIDES development environment with UV..."
uv sync --dev

echo "Development environment setup complete!"
echo "To activate the virtual environment, run: source .venv/bin/activate"
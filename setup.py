from setuptools import find_packages, setup


setup(
    name="treegit",
    version="0.1.0",
    description="A local-only Git-shaped version control system for exploring codebase trees.",
    python_requires=">=3.9",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    entry_points={"console_scripts": ["treegit=treegit.cli:main"]},
)

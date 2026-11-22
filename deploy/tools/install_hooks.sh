#!/bin/sh
# Install the pre-commit guard of this repository (deploy/tools/guard_commit.py)
# into this checkout's .git/hooks. Run once after cloning:
#
#     sh deploy/tools/install_hooks.sh
#
# Hooks are not part of a clone, so every checkout installs its own.
set -e
root="$(git rev-parse --show-toplevel)"
hook="$root/.git/hooks/pre-commit"
cat > "$hook" <<'EOF'
#!/bin/sh
# Installed by deploy/tools/install_hooks.sh: refuses a commit that would
# publish local-only files, private patterns, personal traces or CJK text.
root="$(git rev-parse --show-toplevel)"
py="$root/.venv/bin/python"
[ -x "$py" ] || py=python3
exec "$py" "$root/deploy/tools/guard_commit.py"
EOF
chmod +x "$hook"
echo "installed $hook"

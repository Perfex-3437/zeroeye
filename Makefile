.PHONY: install-hooks

install-hooks:
	@echo "Installing pre-commit hook..."
	@mkdir -p .git/hooks
	@ln -sf ../../tools/pre-commit .git/hooks/pre-commit
	@chmod +x tools/pre-commit
	@echo "✓ Pre-commit hook installed. Run 'git commit' to test."

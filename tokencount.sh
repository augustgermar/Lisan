# Count the number of tokens for the total codebase for reference when cramming it into a context window for review.
# Keep track of the codebase size measured in tokens (including this file).
find . -type f \( -name "*.py" -o -name "*.js" -o -name "*.ts" -o -name "*.md" -o -name "*.json" -o -name "*.yaml" -o -name "*.yml" -o -name "*.html" -o -name "*.css" -o -name "*.sh" -o -name "*.txt" \) -not -path "*/node_modules/*" -not -path "*/.git/*" -not -path "*/dist/*" -not -path "*/build/*" | xargs cat | wc -c | awk '{print "Character count: " $1 " | Estimated tokens: " int($1/4)}'

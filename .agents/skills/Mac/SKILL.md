```markdown
# Mac Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill introduces the key development patterns and conventions found in the "Mac" Python repository. It covers file naming, import/export styles, commit message habits, and testing patterns, providing a practical guide for contributing to or maintaining similar codebases.

## Coding Conventions

### File Naming
- **Convention:** Use camelCase for file names.
- **Example:**  
  `myModule.py`, `dataProcessor.py`

### Import Style
- **Convention:** Use relative imports within modules.
- **Example:**
  ```python
  from .utils import helperFunction
  ```

### Export Style
- **Convention:** Use named exports (explicitly define what is exported).
- **Example:**
  ```python
  __all__ = ['MyClass', 'my_function']
  ```

### Commit Messages
- **Style:** Freeform, no strict prefixes.
- **Average Length:** 64 characters.
- **Example:**  
  `Fix bug in dataProcessor when input is empty`

## Workflows

### Adding a New Module
**Trigger:** When you need to add a new feature or utility.
**Command:** `/add-module`

1. Create a new Python file using camelCase (e.g., `newFeature.py`).
2. Implement your functionality.
3. Use relative imports to reference other modules.
4. Define `__all__` to specify exported functions/classes.
5. Write corresponding tests (see Testing Patterns).
6. Commit changes with a clear, concise message.

### Updating Imports
**Trigger:** When reorganizing or refactoring modules.
**Command:** `/update-imports`

1. Change import statements to use relative paths.
2. Ensure all references are updated accordingly.
3. Run tests to verify nothing is broken.

### Writing Exports
**Trigger:** When exposing specific classes/functions from a module.
**Command:** `/write-exports`

1. At the end of your Python file, define `__all__` with the names to export.
2. Only include necessary functions/classes.
3. Example:
   ```python
   __all__ = ['mainFunction', 'HelperClass']
   ```

## Testing Patterns

- **Framework:** Unknown (not detected).
- **Test File Pattern:** Files end with `.test.ts` (TypeScript test files).
- **Example:**
  ```
  myModule.test.ts
  ```
- **Note:** Although the main code is Python, tests are written in TypeScript, indicating possible cross-language testing or integration with a TypeScript-based test suite. Ensure to follow the `.test.ts` naming convention for new tests.

## Commands
| Command         | Purpose                                    |
|-----------------|--------------------------------------------|
| /add-module     | Scaffold and add a new module              |
| /update-imports | Refactor imports to use relative style     |
| /write-exports  | Define and update named exports in modules |
```

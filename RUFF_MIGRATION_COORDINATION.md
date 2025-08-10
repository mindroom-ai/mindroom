# Ruff Migration Coordination

## Overview
- **Total Errors**: 2,364
- **Unique Error Codes**: 78
- **Strategy**: Fix errors by category in parallel sessions
- **Start Time**: [TO BE FILLED]

## Agent Registry
Each agent should claim their ID by marking the checkbox and adding timestamp:
- [ ] Agent 1: [unclaimed]
- [ ] Agent 2: [unclaimed]
- [ ] Agent 3: [unclaimed]
- [ ] Agent 4: [unclaimed]
- [ ] Agent 5: [unclaimed]
- [ ] Agent 6: [unclaimed]

## Instructions for Each Agent

1. **Claim your agent number** by editing this file first
2. **Select your error category** from the unclaimed ones below
3. **Work on fixing** all instances of your selected error codes
4. **Update progress** in the Progress Log section
5. **Mark complete** when done with your category

## Error Categories by Priority

### CRITICAL - Security & Testing (1,283 errors)
- [ ] **S101** (964 errors) - Use of assert in non-test code - Agent: ___
  - Most common, needs careful review to distinguish test vs production code
- [ ] **S106** (89 errors) - Hardcoded password defaults - Agent: ___
- [ ] **S108** (30 errors) - Probable insecure usage of temp file/directory - Agent: ___
- [ ] **S105** (23 errors) - Hardcoded password string - Agent: ___
- [ ] **SLF001** (165 errors) - Private member accessed - Agent: ___
- [ ] **Other S*** errors** (12 errors: S607, S608, S110, S324, S104, S310) - Agent: ___

### HIGH - Type Annotations (199 errors)
- [ ] **ANN401** (103 errors) - Use of Any type - Agent: ___
- [ ] **ANN201** (70 errors) - Missing return type annotation - Agent: ___
- [ ] **ANN001** (24 errors) - Missing function argument type - Agent: ___
- [ ] **ANN204** (2 errors) - Missing return type for special methods - Agent: ___

### HIGH - Documentation (130 errors)
- [ ] **D400/D415** (46 errors) - First line issues - Agent: ___
- [ ] **D103** (20 errors) - Missing docstring in public function - Agent: ___
- [ ] **D401** (14 errors) - First line imperative mood - Agent: ___
- [ ] **D417** (12 errors) - Missing argument descriptions - Agent: ___
- [ ] **Other D*** errors** (38 errors) - Agent: ___

### MEDIUM - Code Quality (176 errors)
- [ ] **PLR2004** (81 errors) - Magic value comparison - Agent: ___
- [ ] **PLC0415** (54 errors) - Import outside top level - Agent: ___
- [ ] **PLR0913** (20 errors) - Too many arguments - Agent: ___
- [ ] **Other PL*** errors** (21 errors) - Agent: ___

### MEDIUM - Debugging & Printing (153 errors)
- [ ] **T201** (108 errors) - Print statements found - Agent: ___
- [ ] **TRY*** errors** (45 errors) - Exception handling issues - Agent: ___

### MEDIUM - Arguments & Functions (124 errors)
- [ ] **ARG001** (71 errors) - Unused function argument - Agent: ___
- [ ] **ARG002** (45 errors) - Unused method argument - Agent: ___
- [ ] **Other A*** errors** (8 errors) - Agent: ___

### LOW - String Formatting (111 errors)
- [ ] **G004** (110 errors) - Logging f-string usage - Agent: ___
- [ ] **G201** (1 error) - Logging .format() usage - Agent: ___

### LOW - Path Operations (39 errors)
- [ ] **PTH123** (26 errors) - Use Path.open() instead - Agent: ___
- [ ] **PERF401** (8 errors) - List comprehension performance - Agent: ___
- [ ] **PT011** (5 errors) - pytest.raises issues - Agent: ___

### LOW - Miscellaneous (94 errors)
- [ ] **E501** (32 errors) - Line too long - Agent: ___
- [ ] **FBT*** errors** (31 errors) - Boolean trap issues - Agent: ___
- [ ] **RUF*** errors** (23 errors) - Ruff-specific - Agent: ___
- [ ] **Other small categories** (COM812, ERA001, FIX002, etc.) - Agent: ___

## Progress Log
<!-- Add entries in format: [timestamp] Agent X: Fixed CODE in N files, M errors remaining -->

## Completion Checklist
- [ ] All error categories assigned
- [ ] All errors fixed or explicitly ignored
- [ ] Tests passing
- [ ] Pre-commit hooks passing

## Notes
- Run `ruff check --select ALL src/ tests/` to verify your fixes
- Some errors may need to be added to ignore list if they're false positives
- Coordinate in comments if you encounter blockers

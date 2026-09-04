"""backend/corpus_drift — corpus drift audit subsystem.

Measures observed agent behavior against stated claims in .claude/agents/*.md
and CLAUDE.md.  One module per claim; thin CLI hub in scripts/corpus-drift-audit.py.
"""

"""The incident write-up: a local model explains an incident, and never decides anything (r2 §9).

context.py builds the evidence (ruling P17), llm.py talks to Ollama, writeup.py validates the reply
against schema.json and falls back to template.py, and scores.py measures culprit agreement and
grounded numbers.
"""

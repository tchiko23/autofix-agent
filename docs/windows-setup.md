# Windows quick start

Steps:

1. Create the virtual environment
2. Install the dependencies (pip install -r requirements.txt)
3. Copy templates_external_config/config.env.example to templates_external_config/config.env
4. Copy templates_external_config/secrets.env.example to templates_external_config/secrets.env
5. Fill in your GitLab token and Ollama model name
6. Run scripts/check_env.bat to verify the environment
7. Run scripts/dry_run.bat for a no side effect test pass
8. Once the plan looks coherent, run scripts/full_run.bat to create branches and MRs

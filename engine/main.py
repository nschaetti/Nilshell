import sys

if __name__ == "__main__":
    if sys.argv[1:] == ["--tool-runner"]:
        from nilshell_engine.tool_worker import main
    else:
        from nilshell_engine.rpc import main
    main()

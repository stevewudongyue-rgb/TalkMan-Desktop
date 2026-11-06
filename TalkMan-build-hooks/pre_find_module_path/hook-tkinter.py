def pre_find_module_path(hook_api):
    # Tcl/Tk files are supplied explicitly by the project's build script.
    # Do not let the build host's failed Tcl probe exclude Python's tkinter.
    pass

if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        from axon_control import main
        main()
    except Exception:
        import ctypes
        from pathlib import Path
        import traceback
        from user_data import prepare_data
        path = prepare_data(Path(__file__).resolve().parent) / 'startup-error.txt'
        try:
            path.write_text(traceback.format_exc(), encoding='utf-8')
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(None, '启动失败。请查看程序目录中的 startup-error.txt。',
                                         'AXON Control', 0x10)

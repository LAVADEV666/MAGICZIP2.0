"""Run native selectors on their own GUI main thread, never an HTTP worker."""
import json
import sys
import tkinter as tk
from tkinter import filedialog


def main():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    root.update()
    try:
        kind = sys.argv[1]
        if kind == '--self-test':
            chosen = {'tk': root.tk.call('package', 'provide', 'Tk'), 'tcl': root.tk.call('info', 'patchlevel')}
        elif kind == 'files':
            chosen = list(filedialog.askopenfilenames(parent=root, title='Magic ZIP - Agregar archivos', filetypes=[('Todos los archivos', '*.*')]))
        elif kind in {'folder', 'destination'}:
            chosen = filedialog.askdirectory(parent=root, title='Magic ZIP - ' + ('Carpeta de salida' if kind == 'destination' else 'Agregar carpeta'))
        else:
            raise ValueError('Selector desconocido.')
        sys.stdout.reconfigure(encoding='utf-8')
        print(json.dumps(chosen, ensure_ascii=False), flush=True)
    finally:
        root.destroy()


if __name__ == '__main__':
    main()

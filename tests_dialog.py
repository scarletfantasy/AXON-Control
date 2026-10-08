"""Exercise actual Tk dialog construction and validation in a hidden test root."""
import tkinter as tk
import unittest
from unittest.mock import Mock

from axon_protocol import name_bytes
from ui_widgets import ActionDialog, BG


class DialogTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.root.withdraw()
        self.root.configure(bg=BG)
        self.root.update()
        self.addCleanup(self.root.destroy)

    def test_native_dialog_bindings_build_and_invalid_name_stays_editable(self):
        accept = Mock()
        dialog = ActionDialog(self.root, 'Rename', 'Keep parameters', accept,
                              initial='Base', validate=name_bytes)
        self.root.update_idletasks()
        dialog.value.set('名字')
        dialog.submit()
        self.assertTrue(dialog.winfo_exists())
        self.assertTrue(dialog.error.get())
        accept.assert_not_called()
        dialog.value.set('Studio')
        dialog.submit()
        accept.assert_called_once_with('Studio')
        self.assertFalse(dialog.winfo_exists())
        self.assertIsNone(self.root.grab_current())

    def test_escape_releases_modal_grab_without_dispatching_device_action(self):
        accept = Mock()
        dialog = ActionDialog(self.root, 'Reset', 'Backup before reset', accept, warning=True)
        self.assertIs(self.root.grab_current(), dialog)
        self.assertEqual(dialog._escape(), 'break')
        accept.assert_not_called()
        self.assertIsNone(self.root.grab_current())


if __name__ == '__main__':
    unittest.main()

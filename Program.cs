using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            string exeDir = AppDomain.CurrentDomain.BaseDirectory;
            string bat = Path.Combine(exeDir, "run.bat");
            if (!File.Exists(bat))
            {
                MessageBox.Show("run.bat bulunamadi:\n" + bat, "Launcher", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }

            var psi = new ProcessStartInfo
            {
                FileName = "cmd.exe",
                Arguments = "/c \"run.bat\"",
                WorkingDirectory = exeDir,

                // ÖNEMLİ: Shell üzerinden başlat → torun süreçler konsol açabilsin
                UseShellExecute = true,

                // Pencere göstermeden çalıştır
                WindowStyle = ProcessWindowStyle.Hidden,
                CreateNoWindow = true
            };

            Process.Start(psi);
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show("Başlatilamadi:\n" + ex, "Launcher", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
    }
}

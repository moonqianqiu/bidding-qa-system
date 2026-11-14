# One-time grant: give the current account the "Create global objects" user right
# (SeCreateGlobalPrivilege) via the LSA API (LsaAddAccountRights).
#
# Background: portable PostgreSQL for Windows creates its shared memory in the
# Global\ namespace, which a filtered (non-elevated) UAC token cannot do
# (Windows error 1314). Granting this right to the account fixes portable PG
# permanently; it takes effect at next logon.
#
# Usage: launched elevated via grant-elevate.cmd (UAC consent).
# Revert: secpol.msc -> Local Policies / User Rights Assignment ->
#         "Create global objects" -> remove this account.
# NOTE: this file is intentionally pure ASCII (PS 5.1 reads BOM-less files as ANSI).
$ErrorActionPreference = "Stop"
$runDir = Join-Path $PSScriptRoot "..\data\run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$log = Join-Path $runDir "grant-global.log"
"SCRIPT-STARTED user=$env:USERNAME" | Out-File $log -Encoding utf8

$code = @"
using System;
using System.Runtime.InteropServices;

public static class LsaRights
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_UNICODE_STRING
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_OBJECT_ATTRIBUTES
    {
        public int Length;
        public IntPtr RootDirectory;
        public LSA_UNICODE_STRING ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)]
    private static extern uint LsaOpenPolicy(
        ref LSA_UNICODE_STRING SystemName,
        ref LSA_OBJECT_ATTRIBUTES ObjectAttributes,
        uint DesiredAccess,
        out IntPtr PolicyHandle);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)]
    private static extern uint LsaAddAccountRights(
        IntPtr PolicyHandle,
        byte[] AccountSid,
        LSA_UNICODE_STRING[] UserRights,
        uint CountOfRights);

    [DllImport("advapi32.dll")]
    private static extern uint LsaClose(IntPtr ObjectHandle);

    // POLICY_CREATE_ACCOUNT (0x02) | POLICY_LOOKUP_NAMES (0x800)
    private const uint POLICY_ACCESS = 0x00000802;

    public static uint AddRight(string sidString, string right)
    {
        var sid = new System.Security.Principal.SecurityIdentifier(sidString);
        byte[] sidBytes = new byte[sid.BinaryLength];
        sid.GetBinaryForm(sidBytes, 0);

        LSA_UNICODE_STRING rightName = new LSA_UNICODE_STRING();
        rightName.Buffer = Marshal.StringToHGlobalUni(right);
        rightName.Length = (ushort)(right.Length * 2);
        rightName.MaximumLength = (ushort)((right.Length + 1) * 2);

        LSA_UNICODE_STRING systemName = new LSA_UNICODE_STRING();
        systemName.Length = 0;
        systemName.MaximumLength = 0;
        systemName.Buffer = IntPtr.Zero;

        LSA_OBJECT_ATTRIBUTES attrs = new LSA_OBJECT_ATTRIBUTES();
        attrs.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        attrs.RootDirectory = IntPtr.Zero;
        attrs.SecurityDescriptor = IntPtr.Zero;
        attrs.SecurityQualityOfService = IntPtr.Zero;

        IntPtr policyHandle;
        uint status = LsaOpenPolicy(ref systemName, ref attrs, POLICY_ACCESS, out policyHandle);
        if (status != 0) return status;

        try
        {
            LSA_UNICODE_STRING[] rights = new LSA_UNICODE_STRING[1];
            rights[0] = rightName;
            return LsaAddAccountRights(policyHandle, sidBytes, rights, 1);
        }
        finally
        {
            LsaClose(policyHandle);
        }
    }
}
"@

try {
    Add-Type -TypeDefinition $code -Language CSharp

    $sid = ([System.Security.Principal.NTAccount]$env:USERNAME).Translate(
        [System.Security.Principal.SecurityIdentifier]).Value

    $status = [LsaRights]::AddRight($sid, "SeCreateGlobalPrivilege")
    if ($status -ne 0) {
        throw ("LsaAddAccountRights failed, NTSTATUS=0x{0:X8}" -f $status)
    }
    "GRANT-OK sid=$sid (takes effect at next logon)" | Out-File $log -Append -Encoding utf8
}
catch {
    "GRANT-FAIL: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
    exit 1
}

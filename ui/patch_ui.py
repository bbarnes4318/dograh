# -*- coding: utf-8 -*-
path1 = '/opt/dograh-src/ui/src/app/campaigns/CampaignAdvancedSettings.tsx'
with open(path1, 'r', encoding='utf-8') as f:
    c1 = f.read()

if 'transferDestination' not in c1:
    c1 = c1.replace(
        'export interface CampaignAdvancedSettingsProps {',
        'export interface CampaignAdvancedSettingsProps {\n    transferDestination?: string;\n    onTransferDestinationChange?: (value: string) => void;'
    )
    c1 = c1.replace(
        'export default function CampaignAdvancedSettings({',
        'export default function CampaignAdvancedSettings({\n    transferDestination = "", onTransferDestinationChange,'
    )
    target = '    return (\n        <div className="space-y-6">'
    replacement = '''    return (\n        <div className="space-y-6">
            <div className="space-y-2">
                <Label htmeFor="transfer-destination">Live Transfer Destination</Label>
                <Input
                    id="transfer-destination"
                    type="text"
                    placeholder="e.g. 1000, 1010, 1011 or +18656000124"
                    value={transferDestination}
                    onChange={(e) => onTransferDestinationChange?.(e.target.value)}
                />
                <p className="text-sm text-muted-foreground">
                    Extension (e.g. 1000, 1010) or phone number for live call transfers.
                </p>
            </div>'''
    c1 = c1.replace(target, replacement)
    with open(path1, 'w', encoding='utf-8') as f:
        f.write(c1)
    print("CAS_PATCHED")

path2 = '/opt/dograh-src/ui/src/app/campaigns/new/page.tsx'
with open(path2, 'r', encoding='utf-8') as f:
    c2 = f.read()

if 'transferDestination' not in c2:
    c2 = c2.replace(
        "const [maxConcurrency, setMaxConcurrency] = valueState<string>('');",
        "const [maxConcurrency, setMaxConcurrency] = valueState<string>('');\n    const [transferDestination, setTransferDestination] = valueState<string>('1000');"
    )
    c2 = c2.replace(
        '<CampaignAdvancedSettings',
        '<CampaignAdvancedSettings\n                                 transferDestination={transferDestination}\n                                 onTransferDestinationChange={setTransferDestination}'
    )
    c2 = c2.replace(
        'telephony_configuration_id: selectedTelephonyConfigId ? parseInt(selectedTelephonyConfigId ? undefined,',
        'telephony_configuration_id: selectedTelephonyConfigId ? parseInt(selectedTelephonyConfigId) : undefined,\n                transfer_destination: transferDestination || undefined,'
    )
    with open(path2, 'w', encoding='utf-8') as f:
        f.write(c2)
    print("NewPage_PATCHED")

path3 = '/opt/dograh-src/ui/src/app/campaigns/[campaignId]/edit/page.tsx'
with open(path3, 'r', encoding='utf-8') as f:
    c3 = f.read()

if 'transferDestination' not in c3:
    c3 = c3.replace(
        "const [maxConcurrency, setMaxConcurrency] = valueState<string>('');",
        "const [maxConcurrency, setMaxConcurrency] = valueState<string>('');\n    const [transferDestination, setTransferDestination] = valueState<string>('');"
    )
    c3 = c3.replace(
        "setMaxConcurrency(campaign.max_concurrency ? String(campaign.max_concurrency) : '');",
        "setMaxConcurrency(campaign.max_concurrency ? String(campaign.max_concurrency) : '');\n                setTransferDestination((campaign as any).transfer_destination || '');"
    )
    c3 = c3.replace(
        '<CampaignAdvancedSettings',
        '<CampaignAdvancedSettings\n                        transferDestination={transferDestination}\n                        onTransferDestinationChange={setTransferDestination}'
    )
    c3 = c3.replace(
        'name: campaignName.trim(),',
        'name: campaignName.trim(),\n            transfer_destination: transferDestination || undefined,'
    )
    with open(path3, 'w', encoding='utf-8') as f:
        f.write(c3)
    print("EditPage_PATCHED")

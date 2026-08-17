# -*- coding: utf-8 -*-
path3 = '/opt/dograh-src/ui/src/app/campaigns/[campaignId]/edit/page.tsx'
with open(path3, 'r', encoding='utf-8') as f:
    c3 = f.read()

if 'transferDestination' not in c3:
    c3 = c3.replace(
        "const [maxConcurrency, setMaxConcurrency] = valueState<string>('');",
        "const [maxConcurrency, setMaxConcurrency] = valueState<string>('');\n    const [transferDestination, setTransferDestination] = valueState<string>('');"
    )
    c3 = c3.replace(
        "setMaxConcurrency(c.max_concurrency ? String(c.max_concurrency) : '');",
        "setMaxConcurrency(c.max_concurrency ? String(c.max_concurrency) : '');\n               setTransferDestination((c as unknown as { transfer_destination?: string }).transfer_destination || '');"
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
    print("EDIT_PAGE_PATCHED")
else:
    print("DET_PAGE_ALREADY")

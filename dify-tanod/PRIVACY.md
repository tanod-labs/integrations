# Privacy Policy: Tanod Documents plugin

Plugin version 0.1.0. Operator of the service the plugin calls: Tanod, https://tanod.dev. Full service policy: https://tanod.dev/privacy/

## What the plugin sends
When you run a tool, the plugin sends the document you chose (the file's bytes, or the public URL you typed) and the options you set (page range, language) to the Tanod API at https://tanod.dev over HTTPS. This is the only third party the plugin contacts. The plugin has no analytics and no telemetry.

## What the plugin collects and stores
The plugin itself collects nothing and stores nothing. It keeps no copy of your documents or results. Your optional wallet key and price limit are stored by Dify as plugin credentials and are used only on the machine running the plugin.

## Your wallet key
If you enter a Base wallet private key, the plugin uses it locally to sign a payment authorization for one call. The key is never sent to Tanod or anyone else; only the signed authorization is sent, and it is limited to the price shown in the 402 reply, to Tanod's own address, and to your max price setting. The key is never logged or included in any result or error message. Use a dedicated wallet with a small balance. With no key, no payment is ever made.

## What Tanod does with your documents
Per https://tanod.dev/privacy/ and the API documentation: documents are processed in an isolated process and are not kept after the answer is sent, except a result file that you ask for as a download link, which is kept for 24 hours. Tanod does not log document contents. Standard server request logs (time, route, status, IP address) are kept for abuse prevention and operations, as described in the Tanod privacy policy. Bank statements and other documents may contain personal or financial data: only send documents you are entitled to process.

## Results
Text returned by the tools comes from your documents and is untrusted data. The plugin marks it as such in the JSON output.

## Contact
ops@tanod.dev

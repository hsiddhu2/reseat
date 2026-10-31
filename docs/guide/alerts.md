# Email and text alerts through Amazon SNS

**Email and text messages, through Amazon SNS.** Alerts to act on (Book offers, bookings, swaps, a session you hold moving, sign-in needed, offline) can also go to an SNS topic in your own AWS account, with a link to the web app when your phone can reach it. re:Seat stores no AWS keys: it uses an AWS profile you already have.

```bash
pip install "reseat[aws]"
aws sns create-topic --name reseat-alerts --profile <profile>
aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint <your email> --profile <profile>
```

Confirm the subscription from the email AWS sends, then add `sns_topic_arn: <arn>` and `aws_profile: <profile>` to the rules file and restart `reseat serve`. The profile needs `sns:Publish` on that topic only. For text messages, subscribe a phone number with `--protocol sms`. AWS sends US texts only from a registered origination number in your account, set up in AWS End User Messaging SMS, so email is the quick start. The alert text goes to SNS in your account: session codes, titles and the event type, never your token or a session id.

[Back to the README](../../README.md)

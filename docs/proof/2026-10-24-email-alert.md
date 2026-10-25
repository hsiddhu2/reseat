# An email alert through the attendee's own Amazon SNS topic

- **Date and time:** 2026-10-24, about 23:00 PDT
- **re:Seat version:** `86e5740`
- **Where:** the attendee's laptop, with an AWS profile the attendee already had

## What was checked

That re:Seat's SNS path publishes to a topic in the attendee's own AWS account, using a named AWS profile and no keys stored by re:Seat, and that the email subscription delivers it.

## Setup

The attendee created an SNS topic in their AWS account and subscribed their email address to it, then confirmed the subscription from the email AWS sent. re:Seat's rules file gained two lines:

```
sns_topic_arn: "arn:aws:sns:<region>:<account>:<topic>"
aws_profile: <profile>
```

A read of the topic's attributes, before any publish:

```
$ aws sns get-topic-attributes --topic-arn <topic arn> --profile <profile> \
    --query 'Attributes.{confirmed:SubscriptionsConfirmed,pending:SubscriptionsPending}'
{
    "confirmed": "1",
    "pending": "0"
}
```

## Command

One publish through re:Seat's own notifier, the same `publish` call `reseat serve` makes from its background thread:

```
$ python -c "
from reseat import sns
n = sns.SnsNotifier('<topic arn>', profile='<profile>')
ok = n.publish('Test alert', 'This is a test from re:Seat on your laptop. Email alerts are set up. Nothing was booked or changed.')
print('published' if ok else 'failed', n.info())"
published {'sent': 1, 'failures': 0, 'last_error': None}
```

## Output

The attendee received the email, subject "re:Seat: Test alert", body:

```
This is a test from re:Seat on your laptop. Email alerts are set up. Nothing was booked or changed.
```

## Result

The SNS path works end to end: boto3 found the credentials through the named profile, SNS accepted the publish, and the subscription delivered it by email. re:Seat stored no AWS keys.

## What it does not prove

One test message, sent by hand, not an alert the watcher raised, so the queue and thread `reseat serve` uses were not part of it. Text messages were not tried: AWS sends US texts only from a registered origination number, which this account does not have.

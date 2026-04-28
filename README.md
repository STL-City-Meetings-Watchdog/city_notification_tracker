# city_notification_tracker
A website that keeps track of city meeting announcements, the agendas and minutes for those meetings, as well as new permits issued by the city. People can register for notifcations for when new meetings are published and view changes to the minutes and agendas over time. 

## How we made this repo

We exfiltrated the code from running instances. 

We created a tarball of the important files, moved them from the container to the VPS, and from the VPS to our laptops. 

Ur Laptop:
```
ssh -i ~/.ssh/hostinger_ed25519 root@XXX.XXX.XXX.XXX # IP address redacted for security

```

```
docker ps
```
This gets you the instance name, something like `stl-meetings-app-1`. 

```
docker exec stl-meetings-app-1 bash
```
gets you into the container.

```
cd /app
ls # inspect and decide what files to copy, here: requirements.txt main.py templates/
tar -zcf /tmp/snapshot.tgz requirements.txt main.py templates/

```
`exit` out to the VPS:
```
docker cp stl-meetings-app-1:/tmp/snapshot.tgz /tmp/snapshot.tgz
```

`exit` out to laptop shell

```
scp -i ~/.ssh/hostinger_ed25519 root@XXX.XXX.XXX.XXX:/tmp/snapshot.tgz .
mkdir /tmp/snapshot
cd /tmp/snapshot
tar -zxf ../snapshot.tgz
ls # you should see the files!
```
Now you can copy the files to the repo and commit them to github.

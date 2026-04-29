# city_notification_tracker
A website that keeps track of city meeting announcements, the agendas and minutes for those meetings, as well as new permits issued by the city. People can register for notifcations for when new meetings are published and view changes to the minutes and agendas over time. 

## The Hostinger Virtual Private Server

There is a Hostinger "VPS" in the cloud which hosts docker containers. 
Many of the containers scrape city web pages and read city calendar feeds, while also acting as web servers. 

### Onboarding

Dan can invite you to the Hostinger VPS. You'll get an email. Follow the instructions. 

Pro tip: Turn on two-factor auth for your Hostinger account.

#### Have your laptop refer to the the VPS as `veil`
I'm not crazy about documenting the VPS IP address in github, which we may eventually open-source. 
Also it's a pain to type. 
So let's make it so we can refer to the VPS by the domain name `veil`. 

Note the VPS IP address - hereafter referred to as `XXX.XXX.XXX.XXX` - see [the VPS tab](https://hpanel.hostinger.com/vps), click the "Shared with me(1)" pill. 
Add it to your laptop's `/etc/hosts` file with the terminal command:

```
sudo nano /etc/hosts
```
Add a line that looks like
```
XXX.XXX.XXX.XXX veil
```
Note that the middle character there is a tab not a space. Be sure to put a newline at the end of the last line - it's always good luck in these low-level unix files. 


#### Setup your hostinger ssh key

Open up a bash shell and do 

```bash
cd ~/.ssh # where ssh stores its config
ssh-keygen -t ed25519 -C "youremail@gmail.com" # change the email address
```

When prompted save the private key to `hostinger_ed25519`. It will also create a public key at `hostinger_ed25519.pub`. Copy the contents of the public key file to your clipboard, and paste it into the hostinger settings (https://hpanel.hostinger.com/vps/780680/settings).

Now back to the terminal.

```bash
ssh -i ~/.ssh/hostinger_ed25519 root@XXX.XXX.XXX.XXX ls
```
`ssh` should have a minor freakout about connecting to the host for the first time - I accept that - then you should see output approximately like:
```
city_notification_tracker
litellm
main.py
mo-permits
mo-permits 2
mo-permits-app.tgz
mo-permits-v2.tar.gz
mo-permits-v3.tar.gz
mo-permits-v4.tar.gz
npm
openwebui_data.tar
stl-meetings
stl-permits
stl-permits-app.tgz
```
That's what success looks like!

Now also test it with your `veil` alias:

```bash
ssh -i ~/.ssh/hostinger_ed25519 root@veil ls
```
It should be the same.

#### Using `ssh-agent` to automatically supply the `hostinger_ed25519` key

Open up `~/.bashrc` (probably `~/.zprofile` or `~/.zshrc` on mac) and look for something having to do with `ssh-add` or `ssh-agent`. Found nothing? Great! Add this to .bashrc/.zprofile:

```bash
# Start ssh-agent if not already running
if [ -z "$SSH_AGENT_PID" ]; then
    eval "$(ssh-agent -s)" > /dev/null
    ssh-add ~/.ssh/hostinger_ed25519 2>/dev/null
fi
```
This'll tell ssh to look for your hostinger private key. 

Or maybe you did not "find nothing". Did you, like me, find something like this?:

```bash
# Start ssh-agent if not already running
if [ -z "$SSH_AGENT_PID" ]; then
    eval "$(ssh-agent -s)" > /dev/null
    ssh-add ~/.ssh/panam_ed25519 2>/dev/null
fi
```
Add your hostinger key like so:


```bash
# Start ssh-agent if not already running
if [ -z "$SSH_AGENT_PID" ]; then
    eval "$(ssh-agent -s)" > /dev/null
    ssh-add ~/.ssh/panam_ed25519 ~/.ssh/hostinger_ed25519 2>/dev/null
fi
```


That shell config file only runs when you open a new terminal, so just to be sure, do that now. Also in an open terminal, paste in the line `ssh-add ~/.ssh/hostinger_ed25519` without the if guard and the `/dev/null` pipe. It should say something happy like:

```
Identity added: /home/dm/.ssh/hostinger_ed25519 (bob@gmail.com)
```

With all of that done, you should be able to now type:

```
ssh root@veil
```
and see the glorious `root@srv123456:~# ` prompt!

You can do regular unix things like type `ls` to see files or type `exit` to quit out of that bash shell. 


## How we made this repo

This is more of a historical document than the best way to do it now that the code's in Github and you've done the onboarding. 

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

# city_notification_tracker
A website that keeps track of city meeting announcements, the agendas and minutes for those meetings, as well as new permits issued by the city. People can register for notifcations for when new meetings are published and view changes to the minutes and agendas over time. 

## Working with Python

We use `uv` and `direnv` to manage python virtual environments. 
Currently since there are three basically indepenenent projects, each has its own independent python virtual environment. 

At time of writing, the proper version of Python is 3.14.5. This is mentioned numerous times in this file, as well as in the Dockerfiles. All should be synced manually, by you YES YOU typing. 

### Hostinger VPS Configuration
I installed `uv` from their installation page, then installed the correct python version as detailed in the Onboarding section below. 

### Onboarding

1. View the [uv docs](https://docs.astral.sh/uv/getting-started/installation/) and install `uv`.
1. View the [direnv docs](https://direnv.net/docs/installation.html) and install `direnv`. TLDR on Mac: `brew install direnv`. Then do [hook installation](https://direnv.net/docs/hook.html). If you don't know if you're using `bash` or `zsh`, then know that on Linux you're probably using `bash`, and on Mac you're probably using `zsh`. 
1. Have `uv` install the right python version - `3.14.5`. List the available versions with `uv python list` and install the right one with something like `uv python install cpython-3.14.5-linux-x86_64-gnu` (the version string will be different on Mac). 
1. For each of `stl-meetings`, `stl-permits`, and `mo-permits`, `cd` to that directory then:

    ```
    cd app
    uv venv --python cpython-3.14.5-linux-x86_64-gnu # edit the version string according to your OS
    echo ".venv/bin/python" > .python-version
    direnv allow
    uv sync # should print satisfying logs about installing packages
    ```
1. Test it. For each of `stl-meetings`, `stl-permits`, and `mo-permits`, `cd` to that directory's `app` subdir, then examine the output of `which python` - it should be something really long like `/home/dm/city_notification_tracker/stl-meetings/app/.venv/bin/python`, and definitely should not be something system-sounding like `/bin/python` or `/usr/bin/python`. 

### Updating python / uv versions for local dev

Try something along the lines of:

```
uv self update
for d in stl-meetings stl-permits mo-permits; do
  pushd $d/app
  uv venv --python cpython-3.14.5-linux-x86_64-gnu # your version here
  uv sync
  popd
done;

```

### Adding python packages

Use `uv add <packagename>`. Do not - **REPEAT DO NOT** - use `uv pip install <packagename>` - this doesn't
properly update the `pyproject.toml` file and gets everybody's pythons out of sync. 

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


## Ephemeral Testing Environments
You can make branches that will deploy onto the website as a seperate testing version to play with. We are calling it an ephemeral development environment. This is how you 
When you make a new branch for a new issue, check the number of the issue. Name the issue as follows: "fISSUE_NUM-descriptor-of-issue". If the branch does not start with the character "f" followed by some integer, then a ephemeral development environment will not be created. 
For example, if you are making an ephemeral environment for issue 9, you could name your branch "f9-enable-account-creation" or something like that. 

One you have your branch created and have pushed to github, a new ephemeral environment will automatically deploy online. To reach it, go to the url of the following format: subdomain.ISSUE_NUM.dev.veiledprofits.com 
Using the above example, to go to the ephemeral dev environment for the stl meetings subdomain for issue 9, you would go to the following url: stlmeetings.9.dev.veiledprofits.com 
(note that the three available subdomains are as follows: permits, mopermits, and stlmeetings). 

The behavior of the ephemeral testing environments in terms of how they are deployed is handled by a github actions workflow. The yaml file for that workflow is found at .github/workflows/development.yml

For the future, each ephemeral testing environment will spin down after two days, but that is not yet implemented. 


## How to Contribute to the project

For each feature you are working on, please make a development branch. Each feature should map to an issue. If an issue does not already exit over in the issues tab, please make one. If you want to make a ephemeral development environment, follow the instructions in the above subsection. If not, then please use the same format for the branch name as described for making an ephemeral testing environment, but without the character "f" at the beginning. For example, if you want to make a branch to solve issue 9, name it something like "9-enable-account-creation". First, there is the issue number. Then, there is a brief description of the issue. 

Once you have gotten your branch to your liking, you can request to push it to main. To do that, make a pull request, and have at least one other member of this project review and approve your code. You can keep doing review cycles until they approve it. 


### How Ephemeral Testing Environments are Deleted

If we never deleted these environments, they'd eventually take up too many server resources. Therefore the [dev-env-cleanup](./dev-env-cleanup) directory includes code for garbage collecting each development environment after two days, at 3am. This code is not auto-deployed by github actions, because ... well I guess you don't want to wait till its on `main` to test it. 

The implementation is a _systemd service_ that needs files scattered around the VPS's filesystem and runs as `root`. There's a reentrant installer script. If you make changes you need to redeploy thusly:

```
localhost% rsync -av --delete dev-env-cleanup/ root@veil:/tmp/city-dev-env-cleanup/
localhost% ssh root@veil
root@veil:~# cd /tmp/city-dev-env-cleanup/
root@veil:/tmp/city-dev-env-cleanup# ./install 
[output elided for brevity]
```

#### Running the service right meow!

```
systemctl start city-dev-env-cleanup.service
```

#### Checking on the status of the city-dev-env-cleanup service.
```
systemctl status city-dev-env-cleanup.service
```

Or with more detail:

```
journalctl -u city-dev-env-cleanup.service -n 100
```
## Site Monitoring

Prod has basic site monitoring through uptimerobot.com. The account is in Dave's name and the credentials for login have been shared with the other principals. An alert gets sent to an email account controlled by Dave, and he's set up forwarding to send to the other people. 

import asyncio
import json
import os
from unittest.mock import Mock

import pytest
from jupyterhub.objects import Hub, Server
from jupyterhub.orm import Spawner
from kubernetes_asyncio.client.models import (
    V1Capabilities,
    V1Container,
    V1PersistentVolumeClaim,
    V1Pod,
    V1SecurityContext,
)
from traitlets.config import Config

from kubespawner import KubeSpawner


class MockUser(Mock):
    name = 'fake'
    server = Server()

    def __init__(self, **kwargs):
        super().__init__()
        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def escaped_name(self):
        return self.name

    @property
    def url(self):
        return self.server.url


class MockOrmSpawner(Mock):
    name = 'server'
    server = None


async def test_deprecated_config():
    """Deprecated config is handled correctly"""
    with pytest.warns(DeprecationWarning):
        c = Config()
        # both set, non-deprecated wins
        c.KubeSpawner.singleuser_fs_gid = 5
        c.KubeSpawner.fs_gid = 10
        # only deprecated set, should still work
        c.KubeSpawner.hub_connect_ip = '10.0.1.1'
        c.KubeSpawner.singleuser_extra_pod_config = extra_pod_config = {"key": "value"}
        c.KubeSpawner.image_spec = 'abc:123'
        c.KubeSpawner.image_pull_secrets = 'k8s-secret-a'
        spawner = KubeSpawner(hub=Hub(), config=c, _mock=True)
        assert spawner.hub.connect_ip == '10.0.1.1'
        assert spawner.fs_gid == 10
        assert spawner.extra_pod_config == extra_pod_config
        # deprecated access gets the right values, too
        assert spawner.singleuser_fs_gid == spawner.fs_gid
        assert spawner.singleuser_extra_pod_config == spawner.extra_pod_config
        assert spawner.image == 'abc:123'
        assert spawner.image_pull_secrets[0]["name"] == 'k8s-secret-a'


async def test_deprecated_runtime_access():
    """Runtime access/modification of deprecated traits works"""
    spawner = KubeSpawner(_mock=True)
    spawner.singleuser_uid = 10
    assert spawner.uid == 10
    assert spawner.singleuser_uid == 10
    spawner.uid = 20
    assert spawner.uid == 20
    assert spawner.singleuser_uid == 20
    spawner.image_spec = 'abc:latest'
    assert spawner.image_spec == 'abc:latest'
    assert spawner.image == 'abc:latest'
    spawner.image = 'abc:123'
    assert spawner.image_spec == 'abc:123'
    assert spawner.image == 'abc:123'
    spawner.image_pull_secrets = 'k8s-secret-a'
    assert spawner.image_pull_secrets[0]["name"] == 'k8s-secret-a'


async def test_spawner_values():
    """Spawner values are set correctly"""
    spawner = KubeSpawner(_mock=True)

    def set_id(spawner):
        return 1

    assert spawner.uid == None
    spawner.uid = 10
    assert spawner.uid == 10
    spawner.uid = set_id
    assert spawner.uid == set_id
    spawner.uid = None
    assert spawner.uid == None

    assert spawner.gid == None
    spawner.gid = 20
    assert spawner.gid == 20
    spawner.gid = set_id
    assert spawner.gid == set_id
    spawner.gid = None
    assert spawner.gid == None

    assert spawner.fs_gid == None
    spawner.fs_gid = 30
    assert spawner.fs_gid == 30
    spawner.fs_gid = set_id
    assert spawner.fs_gid == set_id
    spawner.fs_gid = None
    assert spawner.fs_gid == None


def check_up(url, ssl_ca=None, ssl_client_cert=None, ssl_client_key=None):
    """Check that a url responds with a non-error code

    For use in exec_python_in_pod,
    which means imports need to be in the function

    Uses stdlib only because requests isn't always available in the target pod
    """
    import ssl
    from urllib import request

    if ssl_ca:
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH, cafile=ssl_ca
        )
        if ssl_client_cert:
            context.load_cert_chain(certfile=ssl_client_cert, keyfile=ssl_client_key)
    else:
        context = None

    # disable redirects (this would be easier if we ran exec in an image with requests)
    class NoRedirect(request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = request.build_opener(NoRedirect, request.HTTPSHandler(context=context))
    try:
        u = opener.open(url)
    except request.HTTPError as e:
        if e.status >= 400:
            raise
        u = e
    print(u.status)


async def test_spawn_start(
    kube_ns,
    kube_client,
    config,
    hub,
    exec_python,
):
    spawner = KubeSpawner(
        hub=hub,
        user=MockUser(name="start"),
        config=config,
        api_token="abc123",
        oauth_client_id="unused",
    )
    # empty spawner isn't running
    status = await spawner.poll()
    assert isinstance(status, int)

    pod_name = spawner.pod_name

    # start the spawner
    url = await spawner.start()

    # verify the pod exists
    pods = (await kube_client.list_namespaced_pod(kube_ns)).items
    pod_names = [p.metadata.name for p in pods]
    assert pod_name in pod_names

    # pod should be running when start returns
    pod = await kube_client.read_namespaced_pod(namespace=kube_ns, name=pod_name)
    assert pod.status.phase == "Running"

    # verify poll while running
    status = await spawner.poll()
    assert status is None

    # make sure spawn url is correct
    r = await exec_python(check_up, {"url": url}, _retries=3)
    assert r == "302"

    # stop the pod
    await spawner.stop()

    # verify pod is gone
    pods = (await kube_client.list_namespaced_pod(kube_ns)).items
    pod_names = [p.metadata.name for p in pods]
    assert pod_name not in pod_names

    # verify exit status
    status = await spawner.poll()
    assert isinstance(status, int)


async def test_spawn_internal_ssl(
    kube_ns,
    kube_client,
    ssl_app,
    hub_pod_ssl,
    hub_ssl,
    config,
    exec_python_pod,
):
    hub_pod_name = hub_pod_ssl.metadata.name

    spawner = KubeSpawner(
        config=config,
        hub=hub_ssl,
        user=MockUser(name="ssl"),
        api_token="abc123",
        oauth_client_id="unused",
        internal_ssl=True,
        internal_trust_bundles=ssl_app.internal_trust_bundles,
        internal_certs_location=ssl_app.internal_certs_location,
    )
    # initialize ssl config
    hub_paths = await spawner.create_certs()

    spawner.cert_paths = await spawner.move_certs(hub_paths)

    # start the spawner
    url = await spawner.start()
    pod_name = "jupyter-%s" % spawner.user.name
    # verify the pod exists
    pods = (await kube_client.list_namespaced_pod(kube_ns)).items
    pod_names = [p.metadata.name for p in pods]
    assert pod_name in pod_names
    # verify poll while running
    status = await spawner.poll()
    assert status is None

    # verify service and secret exist
    secret_name = spawner.secret_name
    secrets = (await kube_client.list_namespaced_secret(kube_ns)).items
    secret_names = [s.metadata.name for s in secrets]
    assert secret_name in secret_names

    service_name = pod_name
    services = (await kube_client.list_namespaced_service(kube_ns)).items
    service_names = [s.metadata.name for s in services]
    assert service_name in service_names

    # resolve internal-ssl paths in hub-ssl pod
    # these are in /etc/jupyterhub/internal-ssl
    hub_ssl_dir = "/etc/jupyterhub"
    hub_ssl_ca = os.path.join(hub_ssl_dir, ssl_app.internal_trust_bundles["hub-ca"])

    # use certipy to resolve these?
    hub_internal = os.path.join(hub_ssl_dir, "internal-ssl", "hub-internal")
    hub_internal_cert = os.path.join(hub_internal, "hub-internal.crt")
    hub_internal_key = os.path.join(hub_internal, "hub-internal.key")

    r = await exec_python_pod(
        hub_pod_name,
        check_up,
        {
            "url": url,
            "ssl_ca": hub_ssl_ca,
            "ssl_client_cert": hub_internal_cert,
            "ssl_client_key": hub_internal_key,
        },
        _retries=3,
    )
    assert r == "302"

    # stop the pod
    await spawner.stop()

    # verify pod is gone
    pods = (await kube_client.list_namespaced_pod(kube_ns)).items
    pod_names = [p.metadata.name for p in pods]
    assert "jupyter-%s" % spawner.user.name not in pod_names

    # verify service and secret are gone
    # it may take a little while for them to get cleaned up
    for i in range(5):
        secrets = (await kube_client.list_namespaced_secret(kube_ns)).items
        secret_names = {s.metadata.name for s in secrets}

        services = (await kube_client.list_namespaced_service(kube_ns)).items
        service_names = {s.metadata.name for s in services}
        if secret_name in secret_names or service_name in service_names:
            await asyncio.sleep(1)
        else:
            break
    assert secret_name not in secret_names
    assert service_name not in service_names


async def test_spawn_progress(kube_ns, kube_client, config, hub_pod, hub):
    spawner = KubeSpawner(
        hub=hub,
        user=MockUser(name="progress"),
        config=config,
    )

    # empty spawner isn't running
    status = await spawner.poll()
    assert isinstance(status, int)

    # start the spawner
    start_future = spawner.start()
    # check progress events
    messages = []
    async for progress in spawner.progress():
        assert 'progress' in progress
        assert isinstance(progress['progress'], int)
        assert 'message' in progress
        assert isinstance(progress['message'], str)
        messages.append(progress['message'])

        # ensure we can serialize whatever we return
        with open(os.devnull, "w") as devnull:
            json.dump(progress, devnull)
    assert 'Started container' in '\n'.join(messages)

    await start_future
    # stop the pod
    await spawner.stop()


async def test_get_pod_manifest_tolerates_mixed_input():
    """
    Test that the get_pod_manifest function can handle a either a dictionary or
    an object both representing V1Container objects and that the function
    returns a V1Pod object containing V1Container objects.
    """
    c = Config()

    dict_model = {
        'name': 'mock_name_1',
        'image': 'mock_image_1',
        'command': ['mock_command_1'],
    }
    object_model = V1Container(
        name="mock_name_2",
        image="mock_image_2",
        command=['mock_command_2'],
        security_context=V1SecurityContext(
            privileged=True,
            run_as_user=0,
            capabilities=V1Capabilities(add=['NET_ADMIN']),
        ),
    )
    c.KubeSpawner.init_containers = [dict_model, object_model]

    spawner = KubeSpawner(config=c, _mock=True)

    # this test ensures the following line doesn't raise an error
    manifest = await spawner.get_pod_manifest()

    # and tests the return value's types
    assert isinstance(manifest, V1Pod)
    assert isinstance(manifest.spec.init_containers[0], V1Container)
    assert isinstance(manifest.spec.init_containers[1], V1Container)


_test_profiles = [
    {
        'display_name': 'Training Env - Python',
        'slug': 'training-python',
        'default': True,
        'kubespawner_override': {
            'image': 'training/python:label',
            'cpu_limit': 1,
            'mem_limit': 512 * 1024 * 1024,
        },
    },
    {
        'display_name': 'Training Env - Datascience',
        'slug': 'training-datascience',
        'kubespawner_override': {
            'image': 'training/datascience:label',
            'cpu_limit': 4,
            'mem_limit': 8 * 1024 * 1024 * 1024,
        },
    },
]


async def test_user_options_set_from_form():
    spawner = KubeSpawner(_mock=True)
    spawner.profile_list = _test_profiles
    # render the form
    await spawner.get_options_form()
    spawner.user_options = spawner.options_from_form(
        {'profile': [_test_profiles[1]['slug']]}
    )
    assert spawner.user_options == {
        'profile': _test_profiles[1]['slug'],
    }
    # nothing should be loaded yet
    assert spawner.cpu_limit is None
    await spawner.load_user_options()
    for key, value in _test_profiles[1]['kubespawner_override'].items():
        assert getattr(spawner, key) == value


async def test_user_options_api():
    spawner = KubeSpawner(_mock=True)
    spawner.profile_list = _test_profiles
    # set user_options directly (e.g. via api)
    spawner.user_options = {'profile': _test_profiles[1]['slug']}

    # nothing should be loaded yet
    assert spawner.cpu_limit is None
    await spawner.load_user_options()
    for key, value in _test_profiles[1]['kubespawner_override'].items():
        assert getattr(spawner, key) == value


async def test_default_profile():
    spawner = KubeSpawner(_mock=True)
    spawner.profile_list = _test_profiles
    spawner.user_options = {}
    # nothing should be loaded yet
    assert spawner.cpu_limit is None
    await spawner.load_user_options()
    for key, value in _test_profiles[0]['kubespawner_override'].items():
        assert getattr(spawner, key) == value


async def test_pod_name_no_named_servers():
    c = Config()
    c.JupyterHub.allow_named_servers = False

    user = Config()
    user.name = "user"

    orm_spawner = Spawner()

    spawner = KubeSpawner(config=c, user=user, orm_spawner=orm_spawner, _mock=True)

    assert spawner.pod_name == "jupyter-user"


async def test_pod_name_named_servers():
    c = Config()
    c.JupyterHub.allow_named_servers = True

    user = Config()
    user.name = "user"

    orm_spawner = Spawner()
    orm_spawner.name = "server"

    spawner = KubeSpawner(config=c, user=user, orm_spawner=orm_spawner, _mock=True)

    assert spawner.pod_name == "jupyter-user--server"


async def test_pod_name_escaping():
    c = Config()
    c.JupyterHub.allow_named_servers = True

    user = Config()
    user.name = "some_user"

    orm_spawner = Spawner()
    orm_spawner.name = "test-server!"

    spawner = KubeSpawner(config=c, user=user, orm_spawner=orm_spawner, _mock=True)

    assert spawner.pod_name == "jupyter-some-5fuser--test-2dserver-21"


async def test_pod_name_custom_template():
    user = MockUser()
    user.name = "some_user"

    pod_name_template = "prefix-{username}-suffix"

    spawner = KubeSpawner(user=user, pod_name_template=pod_name_template, _mock=True)

    assert spawner.pod_name == "prefix-some-5fuser-suffix"


async def test_pod_name_collision():
    user1 = MockUser()
    user1.name = "user-has-dash"

    orm_spawner1 = Spawner()
    orm_spawner1.name = ""

    user2 = MockUser()
    user2.name = "user-has"
    orm_spawner2 = Spawner()
    orm_spawner2.name = "2ddash"

    spawner = KubeSpawner(user=user1, orm_spawner=orm_spawner1, _mock=True)
    assert spawner.pod_name == "jupyter-user-2dhas-2ddash"
    assert spawner.pvc_name == "claim-user-2dhas-2ddash"
    named_spawner = KubeSpawner(user=user2, orm_spawner=orm_spawner2, _mock=True)
    assert named_spawner.pod_name == "jupyter-user-2dhas--2ddash"
    assert spawner.pod_name != named_spawner.pod_name
    assert named_spawner.pvc_name == "claim-user-2dhas--2ddash"
    assert spawner.pvc_name != named_spawner.pvc_name


async def test_spawner_can_use_list_of_image_pull_secrets():
    secrets = ["ecr", "regcred", "artifactory"]

    c = Config()
    c.KubeSpawner.image_spec = "private.docker.registry/jupyter:1.2.3"
    c.KubeSpawner.image_pull_secrets = secrets
    spawner = KubeSpawner(hub=Hub(), config=c, _mock=True)
    assert spawner.image_pull_secrets == secrets

    secrets = [dict(name=secret) for secret in secrets]
    c = Config()
    c.KubeSpawner.image_spec = "private.docker.registry/jupyter:1.2.3"
    c.KubeSpawner.image_pull_secrets = secrets
    spawner = KubeSpawner(hub=Hub(), config=c, _mock=True)
    assert spawner.image_pull_secrets == secrets


async def test_pod_connect_ip(kube_ns, kube_client, config, hub_pod, hub):
    config.KubeSpawner.pod_connect_ip = (
        "jupyter-{username}--{servername}.foo.example.com"
    )

    user = MockUser(name="connectip")
    # w/o servername
    spawner = KubeSpawner(hub=hub, user=user, config=config)

    # start the spawner
    res = await spawner.start()
    # verify the pod IP and port

    assert res == "http://jupyter-connectip.foo.example.com:8888"

    await spawner.stop()

    # w/ servername

    spawner = KubeSpawner(
        hub=hub,
        user=user,
        config=config,
        orm_spawner=MockOrmSpawner(),
    )

    # start the spawner
    res = await spawner.start()
    # verify the pod IP and port

    assert res == "http://jupyter-connectip--server.foo.example.com:8888"
    await spawner.stop()


async def test_get_pvc_manifest():
    c = Config()

    c.KubeSpawner.pvc_name_template = "user-{username}"
    c.KubeSpawner.storage_extra_labels = {"user": "{username}"}
    c.KubeSpawner.storage_selector = {"matchLabels": {"user": "{username}"}}

    spawner = KubeSpawner(config=c, _mock=True)

    manifest = spawner.get_pvc_manifest()

    assert isinstance(manifest, V1PersistentVolumeClaim)
    assert manifest.metadata.name == "user-mock-5fname"
    assert manifest.metadata.labels == {
        "user": "mock-5fname",
        "hub.jupyter.org/username": "mock-5fname",
        "app": "jupyterhub",
        "component": "singleuser-storage",
        "heritage": "jupyterhub",
    }
    assert manifest.spec.selector == {"matchLabels": {"user": "mock-5fname"}}


async def test_variable_expansion(ssl_app):
    """
    Variable expansion not tested here:
    - pod_connect_ip: is tested in test_url_changed
    - user_namespace_template: isn't tested because it isn't set as part of the
      Pod manifest but.
    """
    config_to_test = {
        "pod_name_template": {
            "configured_value": "pod-name-template-{username}",
            "findable_in": ["pod"],
        },
        "pvc_name_template": {
            "configured_value": "pvc-name-template-{username}",
            "findable_in": ["pvc"],
        },
        "secret_name_template": {
            "configured_value": "secret-name-template-{username}",
            "findable_in": ["secret"],
        },
        "storage_selector": {
            "configured_value": {
                "matchLabels": {"dummy": "storage-selector-{username}"}
            },
            "findable_in": ["pvc"],
        },
        "storage_extra_labels": {
            "configured_value": {"dummy": "storage-extra-labels-{username}"},
            "findable_in": ["pvc"],
        },
        "extra_labels": {
            "configured_value": {"dummy": "common-extra-labels-{username}"},
            "findable_value": "common-extra-labels-user1",
            "findable_in": ["pod", "service", "secret"],
        },
        "extra_annotations": {
            "configured_value": {"dummy": "common-extra-annotations-{username}"},
            "findable_value": "common-extra-annotations-user1",
            "findable_in": ["pod", "service", "secret"],
        },
        "working_dir": {
            "configured_value": "working-dir-{username}",
            "findable_in": ["pod"],
        },
        "service_account": {
            "configured_value": "service-account-{username}",
            "findable_in": ["pod"],
        },
        "volumes": {
            "configured_value": [
                {
                    'name': 'volumes-{username}',
                    'secret': {'secretName': 'dummy'},
                }
            ],
            "findable_in": ["pod"],
        },
        "volume_mounts": {
            "configured_value": [
                {
                    'name': 'volume-mounts-{username}',
                    'mountPath': '/tmp/',
                }
            ],
            "findable_in": ["pod"],
        },
        "init_containers": {
            "configured_value": [
                {
                    'name': 'init-containers-{username}',
                    'image': 'busybox',
                }
            ],
            "findable_in": ["pod"],
        },
        "extra_containers": {
            "configured_value": [
                {
                    'name': 'extra-containers-{username}',
                    'image': 'busybox',
                }
            ],
            "findable_in": ["pod"],
        },
        "extra_pod_config": {
            "configured_value": {"schedulerName": "extra-pod-config-{username}"},
            "findable_in": ["pod"],
        },
    }

    c = Config()
    for key, value in config_to_test.items():
        c.KubeSpawner[key] = value["configured_value"]

        if "findable_value" not in value:
            value["findable_value"] = key.replace("_", "-") + "-user1"

    user = MockUser(name="user1")
    spawner = KubeSpawner(
        config=c,
        user=user,
        internal_trust_bundles=ssl_app.internal_trust_bundles,
        internal_certs_location=ssl_app.internal_certs_location,
        _mock=True,
    )
    hub_paths = await spawner.create_certs()
    spawner.cert_paths = await spawner.move_certs(hub_paths)

    manifests = {
        "pod": await spawner.get_pod_manifest(),
        "pvc": spawner.get_pvc_manifest(),
        "secret": spawner.get_secret_manifest("dummy-owner-ref"),
        "service": spawner.get_service_manifest("dummy-owner-ref"),
    }

    for resource_kind, manifest in manifests.items():
        manifest_string = str(manifest)
        for config in config_to_test.values():
            if resource_kind in config["findable_in"]:
                assert config["findable_value"] in manifest_string, (
                    manifest_string
                    + "\n\n"
                    + "finable_value: "
                    + config["findable_value"]
                    + "\n"
                    + "resource_kind: "
                    + resource_kind
                )


async def test_url_changed(kube_ns, kube_client, config, hub_pod, hub):
    user = MockUser(name="url")
    config.KubeSpawner.pod_connect_ip = (
        "jupyter-{username}--{servername}.foo.example.com"
    )
    spawner = KubeSpawner(hub=hub, user=user, config=config)
    spawner.db = Mock()

    # start the spawner
    res = await spawner.start()
    pod_host = "http://jupyter-url.foo.example.com:8888"
    assert res == pod_host

    # Mock an incorrect value in the db
    # Can occur e.g. by interrupting a launch with a hub restart
    # or possibly weird network things in kubernetes
    spawner.server = Server.from_url(res + "/users/url/")
    spawner.server.ip = "1.2.3.4"
    spawner.server.port = 0
    assert spawner.server.host == "http://1.2.3.4:0"
    assert spawner.server.base_url == "/users/url/"

    # poll checks the url, and should restore the correct value
    await spawner.poll()
    # verify change noticed and persisted to db
    assert spawner.server.host == pod_host
    assert spawner.db.commit.call_count == 1
    # base_url should be left alone
    assert spawner.server.base_url == "/users/url/"

    previous_commit_count = spawner.db.commit.call_count
    # run it again, to make sure we aren't incorrectly detecting and committing
    # changes on every poll
    await spawner.poll()
    assert spawner.db.commit.call_count == previous_commit_count

    await spawner.stop()


async def test_delete_pvc(kube_ns, kube_client, hub, config):
    config.KubeSpawner.storage_pvc_ensure = True
    config.KubeSpawner.storage_capacity = '1M'

    spawner = KubeSpawner(
        hub=hub,
        user=MockUser(name="mockuser"),
        config=config,
        _mock=True,
    )
    spawner.api = kube_client

    # start the spawner
    await spawner.start()

    # verify the pod exists
    pod_name = "jupyter-%s" % spawner.user.name
    pods = (await kube_client.list_namespaced_pod(kube_ns)).items
    pod_names = [p.metadata.name for p in pods]
    assert pod_name in pod_names

    # verify PVC is created
    pvc_name = spawner.pvc_name
    pvc_list = (
        await kube_client.list_namespaced_persistent_volume_claim(kube_ns)
    ).items
    pvc_names = [s.metadata.name for s in pvc_list]
    assert pvc_name in pvc_names

    # stop the pod
    await spawner.stop()

    # verify pod is gone
    pods = (await kube_client.list_namespaced_pod(kube_ns)).items
    pod_names = [p.metadata.name for p in pods]
    assert "jupyter-%s" % spawner.user.name not in pod_names

    # delete the PVC
    await spawner.delete_forever()

    # verify PVC is deleted, it may take a little while
    for i in range(5):
        pvc_list = (
            await kube_client.list_namespaced_persistent_volume_claim(kube_ns)
        ).items
        pvc_names = [s.metadata.name for s in pvc_list]
        if pvc_name in pvc_names:
            await asyncio.sleep(1)
        else:
            break
    assert pvc_name not in pvc_names

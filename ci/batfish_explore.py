"""Asks Batfish questions about the snapshot in build/batfish and prints the answers.

Exploration only: it never fails the pipeline. Once we know what Batfish understands of
this network, the answers become strict checks.
"""
import sys
import time

import pandas as pd
from pybatfish.client.session import Session
from pybatfish.datamodel import BgpRoute, BgpRouteConstraints

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 20)
pd.set_option("display.max_colwidth", 90)
pd.set_option("display.max_rows", 200)


def connect(host="localhost", timeout_s=300):
    """Batfish needs a minute or two to start next to the job: retry until it answers."""
    deadline = time.time() + timeout_s
    while True:
        try:
            return Session(host=host)
        except Exception as e:                       # not listening yet
            if time.time() > deadline:
                sys.exit(f"Batfish did not answer within {timeout_s} s: {e}")
            time.sleep(5)


def show(title, ask):
    print(f"\n===== {title} =====")
    try:
        frame = ask().frame()
        print(frame.to_string(index=False) if len(frame) else "(no rows)")
    except Exception as e:                           # keep exploring even if one question fails
        print(f"question failed: {e}")


def route(prefix):
    return BgpRoute(network=prefix, originatorIp="0.0.0.0", originType="igp", protocol="bgp")


bf = connect()
bf.set_network("fabric")
bf.init_snapshot("build/batfish", name="candidate", overwrite=True)
q = bf.q

show("Did Batfish understand every file?", lambda: q.fileParseStatus().answer())
show("Lines it could not read", lambda: q.parseWarning().answer())
show("Problems found while building the model", lambda: q.initIssues().answer())
show("Names used but never defined (typos)", lambda: q.undefinedReferences().answer())
show("Things defined but never used", lambda: q.unusedStructures().answer())
show("Would every BGP session come up?", lambda: q.bgpSessionCompatibility().answer())
show("Routing policies Batfish found on border1",
     lambda: q.namedStructures(nodes="border1", structureTypes="Routing_Policy").answer())

# The proofs: search ALL possible routes for one that a filter lets through but should not
show("TO-ISP: any permitted route other than exactly 10.10.0.0/16?",
     lambda: q.searchRoutePolicies(nodes="/border/", policies="TO-ISP", action="permit",
                                   inputConstraints=BgpRouteConstraints(prefix=["10.10.0.0/16"],
                                                                        complementPrefix=True)).answer())
show("FROM-ISP: any permitted route other than the default route?",
     lambda: q.searchRoutePolicies(nodes="/border/", policies="FROM-ISP", action="permit",
                                   inputConstraints=BgpRouteConstraints(prefix=["0.0.0.0/0"],
                                                                        complementPrefix=True)).answer())
show("DEFAULT-ONLY: any permitted route other than the default route?",
     lambda: q.searchRoutePolicies(nodes="/border/", policies="DEFAULT-ONLY", action="permit",
                                   inputConstraints=BgpRouteConstraints(prefix=["0.0.0.0/0"],
                                                                        complementPrefix=True)).answer())
show("LOOPBACKS-ONLY: any permitted route that is not a switch address?",
     lambda: q.searchRoutePolicies(nodes="/spine|leaf|border/", policies="LOOPBACKS-ONLY", action="permit",
                                   inputConstraints=BgpRouteConstraints(prefix=["10.255.0.0/24:32-32"],
                                                                        complementPrefix=True)).answer())

# Concrete examples through the filter towards the ISP
show("TO-ISP on border1 with three example routes",
     lambda: q.testRoutePolicies(nodes="border1", policies="TO-ISP", direction="out",
                                 inputRoutes=[route("10.10.0.0/16"), route("10.20.20.0/24"),
                                              route("10.10.10.11/32")]).answer())

show("What the ISP receives from us, according to Batfish", lambda: q.bgpRib(nodes="isp").answer())
print("\nExploration finished.")
